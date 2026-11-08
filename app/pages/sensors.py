"""传感器：通道上的传感器台账（读写 sensor 表）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (  # noqa: E402
    danger_button_css,  # noqa: E402
    flash,  # noqa: E402
    kpi_row,
    page_header,
    reset_table_selection,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor.store import (
    channel_reg,  # noqa: E402
    object_registry,  # noqa: E402
    sensors,  # noqa: E402
    tree,  # noqa: E402
)
from lion_monitor.store.sensors import SENSOR_STATUS, SensorError  # noqa: E402

task = store_ctx.current_task()
if task is None:
    page_header("传感器", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    rows = sensors.list_sensors(session, task.id)
    channels = [n for n in tree.list_nodes(session, task.id) if n.node_type == "channel"]
    reg_rows = channel_reg.list_channels(session, task.id)
    reg_codes = [c.code for c in reg_rows]
    free_reg = [c for c in reg_rows if c.is_free]

page_header(
    "传感器",
    "一个通道一台传感器；型号与灵敏度是后续换算 g 值的依据",
    badge=f"{len(rows)} 台",
    demo=False,
)
show_flash()

used = {row.node_id for row in rows}
idle = [ch for ch in channels if ch.id not in used]
kpi_row(
    [
        ("传感器总数", str(len(rows)), None),
        ("启用", str(sum(1 for r in rows if r.status == "启用")), None),
        ("停用/已拆", str(sum(1 for r in rows if r.status != "启用")), None),
        ("自由（未归属对象）", str(sum(1 for r in rows if r.is_free)), None),
    ]
)

left, right = st.columns([0.6, 0.4])
with left:
    section(
        "传感器台账",
        f"共 {len(channels)} 个已装配通道，{len(idle)} 个还没有传感器"
        f"；另有 {len(free_reg)} 个自由通道未装配",
    )
    if rows:
        table = pd.DataFrame(
            [
                {
                    "通道号": r.channel_name,
                    "归属": "自由" if r.is_free else r.object_name,
                    "型号": r.model,
                    "灵敏度": r.sensitivity,
                    "量程": r.range_g,
                    "安装": r.mounting,
                    "标定日期": r.calibrated_on,
                    "状态": r.status,
                }
                for r in rows
            ]
        )
        picked = selectable_table(table, key="sensor_table")
    else:
        st.caption("还没有传感器")
        picked = []

with right:
    if not picked:
        with st.container(border=True):
            section("新增传感器", "通道号手输即可；同一通道可多台，只留一台在用")
            if not channels:
                st.caption("当前任务还没有通道，先去「健康树配置」加通道")
            else:
                with st.form("add_sensor"):
                    ch_code = st.text_input(
                        "通道号",
                        placeholder="如 CH1 / CH5，可留空",
                        help=(
                            "手输即可：能对上健康树通道会自动关联，对不上只记通道号。"
                            f"通道台账现有：{' / '.join(reg_codes) or '（空）'}"
                        ),
                    )
                    model = st.text_input("型号", placeholder="如：IEPE 500mV/g")
                    c1, c2 = st.columns(2)
                    sens = c1.text_input("灵敏度", value="500 mV/g")
                    rng = c2.text_input(
                        "量程",
                        value="±10 g",
                        help="按 g 填写，如 ±10 g（不用 mV 口径）",
                    )
                    c3, c4 = st.columns(2)
                    mount = c3.text_input("安装方式", value="磁吸")
                    cal = c4.text_input("标定日期", value="2026-09-20")
                    status = st.selectbox("状态", SENSOR_STATUS)
                    st.caption("新录入的置「启用」，同通道原有的启用记录自动置「停用」；不限制台数")
                    if st.form_submit_button("保存传感器"):
                        node_id = None
                        try:
                            with store_ctx.open_session() as session:
                                sensors.create_sensor(
                                    session,
                                    task.id,
                                    node_id,
                                    model=model,
                                    sensitivity=sens,
                                    range_g=rng,
                                    mounting=mount,
                                    calibrated_on=cal,
                                    status=status,
                                    channel_code=ch_code,
                                )
                        except SensorError as exc:
                            ui.error(str(exc))
                        else:
                            flash(f"已保存传感器：{ch_code or '未分配'} · {model}")
                            st.rerun()
        st.caption("勾选左侧一行可编辑；勾选多行可批量删除；取消勾选回到新增")
    elif len(picked) == 1:
        b1, b2 = st.columns(2)
        b1.button(
            "＋ 新增传感器",
            on_click=reset_table_selection,
            args=("sensor_table",),
            key="sen_new_mode",
        )
        b2.button(
            "✕ 取消选中",
            on_click=reset_table_selection,
            args=("sensor_table",),
            key="sen_clear_mode",
        )
        row = rows[picked[-1]]
        section(f"📡 {row.channel_name} 传感器", f"ID {row.id}　{row.model}")
        with store_ctx.open_session() as session:
            objects = object_registry.list_objects(session, task.id)
        owner_labels = ["自由（不归属任何监测对象）"] + [
            f"{o.name}（{o.obj_type}）" for o in objects
        ]
        owner_ids: list[int | None] = [None] + [o.id for o in objects]
        current = owner_ids.index(row.object_id) if row.object_id in owner_ids else 0

        # 同一台传感器的保存 / 归属 / 删除都放在这张卡里（马哥：同类功能不要分散）；
        # 2026-11-04 定版：保存修改放最下面，与「删除这台传感器」同一排
        with st.container(border=True):
            ch_code = st.text_input(
                "通道号",
                value=row.channel_code or (row.channel_name if row.node_id else ""),
                key=f"sen_ch_{row.id}",
            )
            model = st.text_input("型号", value=row.model, key=f"sen_model_{row.id}")
            c1, c2 = st.columns(2)
            sens = c1.text_input("灵敏度", value=row.sensitivity, key=f"sen_sens_{row.id}")
            rng = c2.text_input(
                "量程",
                value=row.range_g,
                help="按 g 填写，如 ±10 g（不用 mV 口径）",
                key=f"sen_rng_{row.id}",
            )
            c3, c4 = st.columns(2)
            mount = c3.text_input("安装方式", value=row.mounting, key=f"sen_mount_{row.id}")
            cal = c4.text_input("标定日期", value=row.calibrated_on, key=f"sen_cal_{row.id}")
            status = st.selectbox(
                "状态",
                SENSOR_STATUS,
                index=SENSOR_STATUS.index(row.status) if row.status in SENSOR_STATUS else 0,
                key=f"sen_status_{row.id}",
            )

            col_owner, col_owner_btn = st.columns([0.72, 0.28], vertical_alignment="bottom")
            picked_owner = col_owner.selectbox(
                "归属监测对象", owner_labels, index=current, key=f"owner_{row.id}"
            )
            if col_owner_btn.button("保存归属", key=f"owner_save_{row.id}", width="stretch"):
                target_id = owner_ids[owner_labels.index(picked_owner)]
                try:
                    with store_ctx.open_session() as session:
                        sensors.bind_object(session, row.id, target_id)
                except SensorError as exc:
                    ui.error(str(exc))
                else:
                    flash("已解绑为自由传感器" if target_id is None else f"已绑定到 {picked_owner}")
                    st.rerun()
            st.caption(
                "解绑会把这个通道从健康树上摘下来（进回收站可恢复）；改绑会把通道节点挪到新对象下"
            )

            st.divider()
            # 最下面一排：保存修改 | 删除这台传感器（同一排、真等宽）
            save_col, del_col = st.columns(2, vertical_alignment="center", gap="small")
            if save_col.button("保存修改", key=f"save_sensor_{row.id}", width="stretch"):
                try:
                    with store_ctx.open_session() as session:
                        sensors.update_sensor(
                            session,
                            row.id,
                            channel_code=ch_code,
                            model=model,
                            sensitivity=sens,
                            range_g=rng,
                            mounting=mount,
                            calibrated_on=cal,
                            status=status,
                        )
                except SensorError as exc:
                    ui.error(str(exc))
                else:
                    flash("已保存传感器修改")
                    st.rerun()

            # 删除二合一（与监测对象 / 数据集 / 任务页一致）：第一次变红「确认删除？」，
            # 第二次才真删；armed 状态按传感器 id 分开存，切换选中行不会误删下一台
            one_arm_key = f"del_sensor_armed_{row.id}"
            one_armed = bool(st.session_state.get(one_arm_key))
            with del_col:
                if one_armed:
                    st.markdown(
                        danger_button_css(f"del_sensor_one_btn_{row.id}"),
                        unsafe_allow_html=True,
                    )
                if st.button(
                    "确认删除？" if one_armed else "删除这台传感器",
                    key=f"del_sensor_one_btn_{row.id}",
                    width="stretch",
                    help="点两次删除：第一次进入待确认，第二次执行",
                ):
                    if not one_armed:
                        st.session_state[one_arm_key] = True
                        st.rerun()
                    with store_ctx.open_session() as session:
                        sensors.delete_sensor(session, row.id)
                    st.session_state.pop(one_arm_key, None)
                    flash("已删除 1 台传感器", icon="🗑️")
                    st.rerun()
    else:
        b1, b2 = st.columns(2)
        b1.button(
            "＋ 新增传感器",
            on_click=reset_table_selection,
            args=("sensor_table",),
            key="sen_new_mode_batch",
        )
        b2.button(
            "✕ 取消选中",
            on_click=reset_table_selection,
            args=("sensor_table",),
            key="sen_clear_mode_batch",
        )
        section("批量删除", "已勾选 " + str(len(picked)) + " 台；只删一台请只勾一行")
        chosen = [rows[i] for i in picked]
        st.caption(
            "将删除：" + "、".join(f"{r.channel_name}·{r.model}（{r.status}）" for r in chosen)
        )
        # 批量删除同样二合一：第一次变红「确认删除 N 台？」，第二次执行；
        # armed 里记着当时勾选的 id 集合，勾选一变就自动复位
        batch_ids = tuple(item.id for item in chosen)
        batch_armed = st.session_state.get("del_sensor_batch_armed") == batch_ids
        if batch_armed:
            st.markdown(danger_button_css("del_sensor_btn"), unsafe_allow_html=True)
        if st.button(
            f"确认删除 {len(chosen)} 台？" if batch_armed else f"删除选中的 {len(chosen)} 台",
            key="del_sensor_btn",
            help="点两次删除：第一次进入待确认，第二次执行",
        ):
            if not batch_armed:
                st.session_state["del_sensor_batch_armed"] = batch_ids
                st.rerun()
            with store_ctx.open_session() as session:
                for item in chosen:
                    sensors.delete_sensor(session, item.id)
            st.session_state.pop("del_sensor_batch_armed", None)
            flash(f"已删除 {len(chosen)} 台传感器", icon="🗑️")
            st.rerun()
