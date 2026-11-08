"""监测对象台账（monitor_object）：读写主数据，健康树之后只做引用。

页签：
① 对象台账 —— 对象 / 通道的增删改；
② 频率参数 —— 部件频率台账（CF-1 后端）的界面：按部件列出频率项与参考转速、
   可编辑保存（``hz_at_1rpm`` 同步重算）、附件 CSV 导入（预览 → 确认）与导出配置。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (  # noqa: E402
    danger_button_css,
    flash,
    kpi_row,
    page_header,
    reset_table_selection,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    component_freq,
    object_registry,
    sensors,
    tree,
)
from lion_monitor.store.channel_reg import ChannelError  # noqa: E402
from lion_monitor.store.object_registry import OBJECT_TYPES, ObjectRegistryError  # noqa: E402
from lion_monitor.store.tree import TreeError  # noqa: E402


def render_freq_params(task, objects) -> None:
    """频率参数页签：部件视图 + 编辑保存 + 附件 CSV 导入（预览/确认）+ 导出配置。"""
    if not objects:
        st.caption("台账里还没有监测对象：先在「对象台账」页签新建一个，再录频率参数")
        return

    labels = {f"{row.name}（{row.obj_type}）": row for row in objects}
    picked = st.selectbox("监测对象", list(labels), key="cf_object")
    obj = labels[picked]

    with store_ctx.open_session() as session:
        parts = component_freq.list_parts(session, task.id, object_id=obj.id)
        channels = component_freq.list_channels_of(session, obj.id)
        export_bytes = component_freq.export_csv_bytes(session, task.id, object_id=obj.id)

    st.caption(
        f"部件 {len(parts)} 个 · 频率项 {sum(len(part.points) for part in parts)} 条 · "
        f"通道 {len(channels)} 个"
    )

    col_export, col_import, col_note = st.columns([0.2, 0.2, 0.6], vertical_alignment="center")
    col_export.download_button(
        "⬇ 导出配置",
        data=export_bytes,
        file_name=f"{obj.name}-频率参数.csv",
        mime="text/csv",
        key=f"cf_export_{obj.id}",
        width="stretch",
        disabled=not parts,
    )
    with col_import.popover("⬆ 导入 CSV"):
        st.caption("上传与附件同格式的 CSV（UTF-8 / GBK）→ 先预览，确认后入库")
        upload = st.file_uploader("选择 CSV 文件", type=["csv"], key="cf_upload")
        if upload is not None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "upload.csv"
                path.write_bytes(upload.getvalue())
                try:
                    with store_ctx.open_session() as session:
                        preview = component_freq.preview_import(session, obj.id, path)
                except component_freq.ComponentFreqError as exc:
                    ui.error(f"文件读取失败：{exc}")
                else:
                    st.caption(
                        f"读取 {preview.rows_read} 行 → 部件 {preview.parts} 个、"
                        f"频率项 {preview.records} 条：将新增 {preview.created}、"
                        f"将更新 {preview.updated}、未变 {preview.unchanged}、"
                        f"跳过 {preview.skipped} 行"
                    )
                    writable = preview.created + preview.updated
                    ui.warning(f"将更新 {writable} 项，可能影响标记线显示")
                    if preview.skipped_details:
                        with st.expander(f"查看跳过原因（{preview.skipped} 行）", expanded=False):
                            for detail in preview.skipped_details:
                                st.caption(f"· {detail}")
                    confirmed = st.checkbox("确认导入", key="cf_import_confirm")
                    if st.button(
                        "确认导入",
                        key="cf_import_btn",
                        disabled=not confirmed,
                    ):
                        try:
                            with store_ctx.open_session() as session:
                                summary = component_freq.import_csv(session, obj.id, path)
                        except component_freq.ComponentFreqError as exc:
                            ui.error(f"导入失败：{exc}")
                        else:
                            flash(summary.describe())
                            st.rerun()
    col_note.caption(
        "导入与「测量附件」同列序；参考转速由该部件 OM1×60 反推；"
        "同一部件的频率项只存一条，挂多通道用通道号多值"
    )

    if not parts:
        st.caption("还没有频率参数：可导入附件 CSV，或先按下面对象逐个录入")
        st.info("先导入一次附件 CSV 就能得到全部部件；之后在这里逐个微调。", icon="ℹ️")
        return

    part_labels = {f"{part.part_code} · {part.part_name}": part for part in parts}
    picked_part = st.selectbox("部件", list(part_labels), key="cf_part")
    part = part_labels[picked_part]

    ref_text = f"{part.ref_rpm:g} rpm" if part.ref_rpm else "未填"
    st.caption(f"类型：{part.part_type or '—'}　参考转速：{ref_text}")
    st.caption(f"已挂通道：{part.channels_text or '—'}")
    missing = [kind for kind in component_freq.FREQ_KINDS if kind not in part.points]
    if missing:
        st.caption(
            "未配置：" + " / ".join(component_freq.FREQ_KIND_COLUMNS[kind] for kind in missing)
        )

    kinds = list(part.points)
    with st.form(f"cf_form_{obj.id}_{part.part_code}"):
        ref_col, ref_note = st.columns([0.3, 0.7], vertical_alignment="bottom")
        ref_rpm = ref_col.number_input(
            "参考转速（rpm）",
            min_value=0.0,
            value=float(part.ref_rpm or 0.0),
            step=10.0,
            key=f"cf_ref_{part.part_code}",
        )
        ref_note.caption("改参考转速会重算该部件所有频率项的归一化值")
        values: dict[str, float | None] = {}
        scalings: dict[str, str] = {}
        scale_labels = list(component_freq.SCALING_LABELS.values())
        scale_codes = {label: code for code, label in component_freq.SCALING_LABELS.items()}
        for start in range(0, len(kinds), 2):
            cols = st.columns(2)
            for col, kind in zip(cols, kinds[start : start + 2], strict=False):
                point = part.points[kind]
                with col:
                    values[kind] = st.number_input(
                        f"{component_freq.FREQ_KIND_COLUMNS[kind]}（Hz）",
                        min_value=0.0,
                        value=float(point.value_hz),
                        step=1.0,
                        key=f"cf_val_{part.part_code}_{kind}",
                        format="%.4f",
                    )
                    label = component_freq.SCALING_LABELS.get(point.scaling, scale_labels[0])
                    scalings[kind] = scale_codes[
                        st.selectbox(
                            f"{component_freq.FREQ_KIND_COLUMNS[kind]} 缩放",
                            scale_labels,
                            index=scale_labels.index(label),
                            key=f"cf_scale_{part.part_code}_{kind}",
                            label_visibility="collapsed",
                        )
                    ]
                    order = point.order
                    if order is not None:
                        st.caption(f"阶次 {order:.4g}×")
        st.caption("频率值单位 Hz；阶次 = 值 ÷ 参考转速 × 60；「固定值」类不随转速缩放")
        if st.form_submit_button("保存频率参数"):
            try:
                with store_ctx.open_session() as session:
                    changed = component_freq.save_part(
                        session,
                        obj.id,
                        part.part_code,
                        ref_rpm=ref_rpm if ref_rpm > 0 else None,
                        values=values,
                        scalings=scalings,
                    )
            except component_freq.ComponentFreqError as exc:
                ui.error(str(exc))
            else:
                flash(f"已保存：{part.part_code}（改动 {changed} 项）")
                st.rerun()


task = store_ctx.current_task()
if task is None:
    page_header("监测对象", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    rows = object_registry.list_objects(session, task.id)
    chans = channel_reg.list_channels(session, task.id)
    nodes = tree.list_nodes(session, task.id)
    sensor_rows = sensors.list_sensors(session, task.id)

#: 已经装配到健康树上的台账对象（组件节点的 ref_id）——台账只做标记，不改装配关系
assembled = {
    node.ref_id
    for node in nodes
    if node.node_type == "component" and node.ref_type == "monitor_object"
}

by_object: dict[int, list[str]] = {}
for ch in chans:
    by_object.setdefault(ch.object_id, []).append(ch.code)

page_header(
    "监测对象",
    "主数据台账：先在这里把对象建好，再到「健康树配置」里装配",
    badge=f"{len(rows)} 个对象",
    demo=False,
)
show_flash()

kpi_row(
    [
        ("监测对象", str(len(rows)), None),
        ("通道", str(len(chans)), "来自通道台账"),
        ("类型分布", str(len({r.obj_type for r in rows})), "轴承/电机/气缸…"),
        ("已装配", str(len(assembled)), "其余为自由对象"),
    ]
)

tab_objects, tab_freq = st.tabs(["对象台账", "频率参数"], key="object_tabs")

with tab_freq:
    render_freq_params(task, rows)

with tab_objects:
    left, right = st.columns([0.58, 0.42])

    with left:
        section("对象台账", "选中一行后在右侧修改或删除")
        if rows:
            table = pd.DataFrame(
                [
                    {
                        "名称": r.name,
                        "类型": r.obj_type or "—",
                        "部位": r.part or "—",
                        "通道": " / ".join(by_object.get(r.id, [])) or "—",
                        "装配": "已装配" if r.id in assembled else "未装配",
                    }
                    for r in rows
                ]
            )
            picked = selectable_table(table, key="object_table")
        else:
            st.caption("台账为空：在右侧新建，或去「健康树配置」导入模板后同步")
            picked = []

    with right:
        if picked:
            row = rows[picked[-1]]
            b1, b2 = st.columns(2)
            b1.button(
                "＋ 新建对象",
                on_click=reset_table_selection,
                args=("object_table",),
                key="obj_new_mode",
            )
            b2.button(
                "✕ 取消选中",
                on_click=reset_table_selection,
                args=("object_table",),
                key="obj_clear_mode",
            )
            section(f"⚙️ {row.name}", f"ID {row.id}　{row.obj_type}")
            idx = OBJECT_TYPES.index(row.obj_type) if row.obj_type in OBJECT_TYPES else 0
            new_name = st.text_input("名称", value=row.name, key=f"obj_name_{row.id}")
            cols = st.columns(2)
            new_type = cols[0].selectbox(
                "对象类型", OBJECT_TYPES, index=idx, key=f"obj_type_{row.id}"
            )
            new_part = cols[1].text_input("部位", value=row.part, key=f"obj_part_{row.id}")
            new_remark = st.text_area("备注", value=row.remark, height=60, key=f"obj_note_{row.id}")

            # 保存 / 删除同一排、真等宽；删除二合一（第一次变红「确认删除？」，第二次执行）
            obj_arm_key = f"del_obj_armed_{row.id}"
            obj_armed = bool(st.session_state.get(obj_arm_key))
            c_save, c_del = st.columns(2, vertical_alignment="center", gap="small")
            if c_save.button("保存修改", key=f"save_obj_{row.id}", width="stretch"):
                try:
                    with store_ctx.open_session() as session:
                        object_registry.update_object(
                            session,
                            row.id,
                            name=new_name,
                            obj_type=new_type,
                            part=new_part,
                            remark=new_remark,
                        )
                except ObjectRegistryError as exc:
                    ui.error(str(exc))
                else:
                    st.session_state.pop(obj_arm_key, None)
                    flash(f"已保存：{new_name}")
                    st.rerun()
            with c_del:
                if obj_armed:
                    st.markdown(
                        danger_button_css(f"del_obj_btn_{row.id}"),
                        unsafe_allow_html=True,
                    )
                if st.button(
                    "确认删除？" if obj_armed else "删除对象",
                    key=f"del_obj_btn_{row.id}",
                    width="stretch",
                    help="点两次删除：第一次进入待确认，第二次执行；通道会回到自由态",
                ):
                    if not obj_armed:
                        st.session_state[obj_arm_key] = True
                        st.rerun()
                    with store_ctx.open_session() as session:
                        released = object_registry.delete_object(session, row.id)
                    st.session_state.pop(obj_arm_key, None)
                    flash(
                        f"已从台账删除；{released} 个通道已回自由态"
                        if released
                        else "已从台账删除",
                        icon="🗑️",
                    )
                    st.rerun()
            if obj_armed:
                st.markdown(
                    ":red[再点一次「确认删除？」即删除；删除只从台账移除，"
                    "通道回自由态，已装配到健康树的节点需在健康树里处理。]"
                )

            # ---------------- 通道设置 ----------------
            # 本对象在树上对应的监测对象节点（装配通道要挂在它下面）
            comp_node = next(
                (
                    item
                    for item in nodes
                    if item.node_type == "component"
                    and item.ref_type == "monitor_object"
                    and item.ref_id == row.id
                ),
                None,
            )
            # 一条通道可以同时装在多个对象下（现场），台账 object_id 只记一个 →
            # 「本对象的通道」要按树算：台账归属是本对象，或树里本对象节点下装了它
            tree_reg_ids = {
                item.ref_id
                for item in nodes
                if comp_node is not None
                and item.parent_id == comp_node.id
                and item.node_type == "channel"
                and item.ref_type == "channel_reg"
            }
            mine = [ch for ch in chans if ch.object_id == row.id or ch.id in tree_reg_ids]
            section("📡 通道设置", f"{len(mine)} 个通道挂在「{row.name}」下")

            if mine:
                # 管理已有通道直接占原来「通道展示」的位置：选一条 → 改 → 保存/删除同排
                labels = [f"{c.code}" + (f"（{c.remark}）" if c.remark else "") for c in mine]
                pick = st.selectbox("管理已有通道", labels, key=f"ch_pick_{row.id}")
                # 旧选中值可能已不在选项里（刚删过通道 / 改过名），兜底回第一条，别抛 ValueError
                cur = next(
                    (c for c, label in zip(mine, labels, strict=True) if label == pick), mine[0]
                )
                ch_cols = st.columns(2)
                upd_code = ch_cols[0].text_input("通道号", value=cur.code, key=f"ch_code_{row.id}")
                upd_remark = ch_cols[1].text_input(
                    "备注", value=cur.remark, key=f"ch_note_{row.id}"
                )

                ch_arm_key = f"del_ch_armed_{row.id}"
                ch_armed = bool(st.session_state.get(ch_arm_key))
                c_ch_save, c_ch_del = st.columns(2, vertical_alignment="center", gap="small")
                if c_ch_save.button("保存通道", key=f"save_ch_{row.id}", width="stretch"):
                    try:
                        with store_ctx.open_session() as session:
                            channel_reg.update_channel(
                                session, cur.id, code=upd_code, remark=upd_remark
                            )
                    except ChannelError as exc:
                        ui.error(str(exc))
                    else:
                        st.session_state.pop(ch_arm_key, None)
                        flash(f"已保存通道：{upd_code}")
                        st.rerun()
                with c_ch_del:
                    if ch_armed:
                        st.markdown(
                            danger_button_css(f"del_ch_btn_{row.id}"),
                            unsafe_allow_html=True,
                        )
                    if st.button(
                        "确认删除？" if ch_armed else "删除通道",
                        key=f"del_ch_btn_{row.id}",
                        width="stretch",
                        help="点两次删除：第一次进入待确认，第二次执行",
                    ):
                        if not ch_armed:
                            st.session_state[ch_arm_key] = True
                            st.rerun()
                        with store_ctx.open_session() as session:
                            channel_reg.delete_channel(session, cur.id)
                        st.session_state.pop(ch_arm_key, None)
                        flash("已从通道台账删除", icon="🗑️")
                        st.rerun()
            else:
                st.caption("该对象还没有通道；下面新增后，健康树装配时才能选到")

            # 新增通道：通道号来自「传感器台账」——下拉选一台，装配到本对象下
            # 树是归属的唯一真相：只建台账行不装配，刷新后会被
            # _free_unassembled_channels 清回自由态
            used_refs = {
                (item.ref_type, item.ref_id)
                for item in nodes
                if comp_node is not None and item.parent_id == comp_node.id
            }
            reg_by_code = {ch.code: ch for ch in chans}
            candidates = {
                f"{item.channel_code} · {item.model or '未填型号'}": item
                for item in sensor_rows
                if item.channel_code
                and item.status != "已拆"
                and (
                    reg_by_code.get(item.channel_code) is None
                    or ("channel_reg", reg_by_code[item.channel_code].id) not in used_refs
                )
            }
            add_cols = st.columns([0.66, 0.34], vertical_alignment="bottom")
            if comp_node is None:
                add_cols[0].caption(
                    "该对象还没装配到健康树：先在「健康树配置」把它加上，才能往它下面挂通道"
                )
                add_cols[1].button(
                    "新增通道", key=f"add_ch_{row.id}", width="stretch", disabled=True
                )
            elif not candidates:
                add_cols[0].caption("传感器台账里没有可用传感器了，先去「传感器」页新增一台")
                add_cols[1].button(
                    "新增通道", key=f"add_ch_{row.id}", width="stretch", disabled=True
                )
            else:
                picked_sensor = add_cols[0].selectbox(
                    "已有传感器", list(candidates), key=f"ch_from_sensor_{row.id}"
                )
                chosen = candidates[picked_sensor]
                if add_cols[1].button("新增通道", key=f"add_ch_{row.id}", width="stretch"):
                    try:
                        with store_ctx.open_session() as session:
                            reg = reg_by_code.get(chosen.channel_code)
                            if reg is None:
                                reg = channel_reg.create_channel(
                                    session, task.id, None, chosen.channel_code
                                )
                            tree.create_node(
                                session,
                                task.id,
                                "channel",
                                reg.code,
                                parent_id=comp_node.id,
                                ref_type="channel_reg",
                                ref_id=reg.id,
                            )
                    except (ChannelError, TreeError) as exc:
                        ui.error(str(exc))
                    else:
                        flash(f"已装配通道：{reg.code}（{chosen.model or '未填型号'}）")
                        st.rerun()

        else:
            with st.container(border=True):
                section("新增监测对象", "未选中行时显示；勾选左侧任一行即切换为详情")
                with st.form("add_object"):
                    name = st.text_input("对象名称", placeholder="如：隔膜过辊轴承")
                    cols = st.columns(2)
                    obj_type = cols[0].selectbox("对象类型", OBJECT_TYPES)
                    part = cols[1].text_input("部位", placeholder="如：隔膜过辊")
                    remark = st.text_area("备注", height=60)
                    if st.form_submit_button("创建对象"):
                        try:
                            with store_ctx.open_session() as session:
                                created = object_registry.create_object(
                                    session, task.id, name, obj_type, part, remark
                                )
                        except ObjectRegistryError as exc:
                            ui.error(str(exc))
                        else:
                            flash(f"已创建：{created.name}（{created.obj_type}）")
                            st.rerun()
