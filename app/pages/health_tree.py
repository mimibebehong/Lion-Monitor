"""健康树配置：读写 task_node（数据库）。

层级：任务 → 设备 → 监测对象 → 通道 → 监测指标；阈值不在树里，去「阈值设置」配。
支持新增子节点、改名改属性、级联软删、模板导出/导入（整树替换）、恢复内置示例树。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metric_params_ui  # noqa: E402
import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import streamlit.components.v1  # noqa: E402,F401
import tree_ui  # noqa: E402
import ui
from ui import (  # noqa: E402
    danger_button_css,
    flash,
    hint,
    kpi_row,
    kv_block,
    page_header,
    section,
    show_flash,
)

from lion_monitor.constants import METRIC_UNITS  # noqa: E402
from lion_monitor.features.health import NodeHealth, node_health  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    bootstrap,
    channel_reg,
    component_freq,
    metric_assign,
    metric_def,
    object_registry,
    sensors,
    tree,
    tree_template,
)
from lion_monitor.store.channel_reg import ChannelError  # noqa: E402
from lion_monitor.store.metric_assign import MetricAssignError  # noqa: E402
from lion_monitor.store.object_registry import OBJECT_TYPES, ObjectRegistryError  # noqa: E402
from lion_monitor.store.sensors import SensorError  # noqa: E402
from lion_monitor.store.tree import CHILD_OF, NODE_TYPE_TEXT, TreeError  # noqa: E402

TYPE_ICON = {
    "task": "🗂",
    "device": "🏭",
    "component": "⚙️",
    "channel": "📡",
    "metric": "📐",
}
METRIC_CHOICES = tuple(METRIC_UNITS)

#: 通道下拉框里"新建"这一项（同一个控件，不再另开一个入口）
NEW_CHANNEL_OPTION = "＋ 新建通道…"

#: 转速来源（``params.resolve_rpm`` 的 source 口径）→ 界面文案
RPM_SOURCE_TEXT = {
    "node": "节点手填",
    "measured": "批次实测",
    "rated": "对象额定",
    "given": "手动给定",
}


def _derived_hz(point: component_freq.FreqPoint, rpm: float | None) -> float | None:
    """对象台账派生值：``fixed`` 直接取值，``speed`` 按有效转速缩放。"""
    if point.scaling == "fixed":
        return point.value_hz
    if rpm is None:
        return None
    return point.hz_at_1rpm * rpm


def _render_add_channel(node, nodes, ch_pool, sensor_models) -> None:
    """监测对象节点下装配传感器通道：一个下拉框选已有，选「＋ 新建通道…」就地新建。

    （外层已有「＋ 新增{NODE_TYPE_TEXT[child_type]}」展开条，这里不再套一层）

    同一台传感器通道可以挂到多个监测对象下，所以下拉框列出台账里**全部**通道，
    只排除当前节点下已经装过的那些。
    """
    used_refs = {(child.ref_type, child.ref_id) for child in nodes if child.parent_id == node.id}
    pool = [row for row in ch_pool if ("channel_reg", row.id) not in used_refs]
    # 标签只写「通道号 · 传感器型号」：一台传感器可能同时监测多个对象，写上归属会挤爆
    labels = [f"{row.code} · {sensor_models.get(row.id) or '未挂传感器'}" for row in pool] + [
        NEW_CHANNEL_OPTION
    ]

    chosen = st.selectbox("选择传感器通道（也可选最后一项新建）", labels, key=f"pc_{node.id}")
    new_code = ""
    if chosen == NEW_CHANNEL_OPTION:
        new_code = st.text_input("新通道号", placeholder="如：CH5", key=f"pcnew_{node.id}").strip()

    if not st.button("装配通道", key=f"addch_{node.id}"):
        return

    parent_ref = node.ref_id if node.ref_type == "monitor_object" else None
    try:
        with store_ctx.open_session() as session:
            if chosen == NEW_CHANNEL_OPTION:
                if not new_code:
                    ui.error("请填写新通道号")
                    return
                row_new = channel_reg.create_channel(session, node.task_id, parent_ref, new_code)
            else:
                row_new = pool[labels.index(chosen)]
            tree.create_node(
                session,
                node.task_id,
                "channel",
                row_new.code,
                parent_id=node.id,
                ref_type="channel_reg",
                ref_id=row_new.id,
            )
    except (ChannelError, TreeError) as exc:
        ui.error(str(exc))
    else:
        # 重跑后保持展开条打开，并把视口滚回这个节点（现场：别刷新完弹回顶部）
        st.session_state[f"ht_addopen_{node.id}"] = True
        flash(f"已装配通道：{row_new.code}（传感器可挂到多个监测对象）")
        st.rerun()


def _ref_missing(task, node) -> bool:
    """节点引用的台账行是否已经不存在（悬空引用）。"""
    if node.ref_id is None:
        return False
    loaders = {
        "monitor_object": object_registry.list_objects,
        "channel_reg": channel_reg.list_channels,
        "metric_def": metric_def.list_metrics,
    }
    loader = loaders.get(node.ref_type)
    if loader is None:
        return False
    with store_ctx.open_session() as session:
        return node.ref_id not in {row.id for row in loader(session, task.id)}


def render_freq_block(task, node) -> None:
    """节点属性里的特征频率区：台账派生值 + 本节点手填覆盖（手填优先）。"""
    with store_ctx.open_session() as session:
        object_id = component_freq.object_id_of_node(session, node.id)
        if object_id is None:
            st.caption("未关联监测对象：无法派生特征频率，请把该节点引用到「监测对象」台账")
            return
        parts = component_freq.list_parts(session, task.id, object_id=object_id)
        overrides = component_freq.node_freq_overrides(session, node.id)
        rpm, rpm_source = component_freq.resolve_node_rpm(session, node.id)

    if not parts:
        st.caption("该对象还没有频率参数：去「监测对象 → 频率参数」录入或导入 CSV")
        return

    section("📐 特征频率", "对象台账派生 + 本节点手填覆盖（手填优先，不随转速缩放）")
    rpm_text = f"{rpm:g} rpm" if rpm else "未配置"
    st.caption(f"转速来源：{RPM_SOURCE_TEXT.get(rpm_source or '', '未配置')}（{rpm_text}）")

    table: list[dict[str, object]] = []
    options: list[str] = []
    targets: list[tuple[str, str]] = []
    for part in parts:
        for kind, point in part.points.items():
            derived = _derived_hz(point, rpm)
            override = overrides.get((part.part_code, kind))
            if override is not None:
                source = "节点手填"
            elif derived is not None and derived > 0:
                source = "对象派生"
            else:
                source = "未配置"
            column = component_freq.FREQ_KIND_COLUMNS[kind]
            table.append(
                {
                    "部件": part.part_code,
                    "频率项": column,
                    "对象派生 (Hz)": None if derived is None else round(derived, 4),
                    "节点手填 (Hz)": None if override is None else round(override, 4),
                    "来源": source,
                }
            )
            options.append(
                f"{part.part_code} · {column}（{component_freq.FREQ_KIND_LABELS[kind]}）"
            )
            targets.append((part.part_code, kind))

    st.dataframe(pd.DataFrame(table), hide_index=True, height=180)

    with st.form(f"freq_override_{node.id}"):
        picked = st.selectbox("覆盖项", options, key=f"fo_pick_{node.id}")
        value = st.number_input(
            "覆盖值（Hz）",
            min_value=0.0,
            value=0.0,
            step=1.0,
            key=f"fo_value_{node.id}",
            help="填实际频率值；保存后该节点按此值画标记线，与转速无关",
        )
        col_save, col_clear = st.columns(2)
        save_clicked = col_save.form_submit_button("保存覆盖")
        clear_clicked = col_clear.form_submit_button("清除覆盖")
        st.caption("节点手填按固定值处理；要改转速请用上面的「转速」属性")

    if not (save_clicked or clear_clicked):
        return
    part_code, kind = targets[options.index(picked)]
    try:
        with store_ctx.open_session() as session:
            component_freq.set_node_freq_override(
                session, node.id, part_code, kind, None if clear_clicked else value
            )
    except component_freq.ComponentFreqError as exc:
        ui.error(str(exc))
    else:
        action = "清除" if clear_clicked else "保存"
        flash(f"已{action}覆盖：{part_code} {component_freq.FREQ_KIND_COLUMNS[kind]}", icon="📐")
        st.rerun()


def _report_text(report) -> str:
    """导入完成报告：新建节点数、绑定指标数、写阈值数、跳过及原因。"""
    head = f"新建节点 {report.node_total} 个"
    if report.node_counts:
        detail = "／".join(
            f"{NODE_TYPE_TEXT[key]} {report.node_counts[key]}"
            for key in NODE_TYPE_TEXT
            if report.node_counts.get(key)
        )
        if detail:
            head += f"（{detail}）"
    bits = [
        head,
        f"绑定指标 {report.metrics_bound} 个",
        f"写阈值 {report.thresholds_written} 条",
    ]
    if report.thresholds_skipped:
        bits.append(f"阈值跳过 {report.thresholds_skipped} 条")
    if report.params_overridden:
        bits.append(f"按生效值搬运参数 {report.params_overridden} 个指标")
    text = "；".join(bits)
    if report.notice:
        text += f"\n{report.notice}"
    if report.skipped:
        shown = "；".join(report.skipped[:5])
        tail = f" 等共 {len(report.skipped)} 条" if len(report.skipped) > 5 else ""
        text += f"\n跳过原因：{shown}{tail}"
    return text


def _reset_node_delete_arm(node_id: int) -> None:
    """切页签即撤掉该节点的删除待确认态，避免回到「改名与删除」时按钮还红着。"""
    st.session_state.pop(f"del_node_armed_{node_id}", None)


def _apply_health(node: dict, health: dict[int, NodeHealth]) -> None:
    """把 ``features.health.node_health`` 的真实状态注入树快照。

    ``tree_ui.state_of`` 只认 ``node['state']``，不注入就一律回落「未设阈值」——
    阈值明明配了、指标也有数据，页面上却全是「未设阈值」（2026-11-05 报的 bug）。
    指标层按最新值判级，设备/对象/通道沿子树汇总，阈值节点本身仍显示「—」。
    """
    item = health.get(int(node["id"]))
    node["state"] = item.state if item is not None else "unset"
    for child in node.get("children") or []:
        _apply_health(child, health)


task = store_ctx.current_task()
if task is None:
    page_header(
        "健康树配置", "还没有测试任务：去「任务列表」新建，或直接用内置示例树起步", demo=False
    )
    st.info("库里还没有测试任务。新建任务后在这里配置健康树。", icon="🌳")
    if st.button("恢复内置示例树", key="ht_reset_example"):
        with store_ctx.open_session() as session:
            created = bootstrap.bootstrap_example_task(session)
        if created is None:
            ui.warning("库里已有测试任务，未重复建示例：请去「任务列表」选择任务。")
        else:
            store_ctx.set_current_task(created.id)
            flash(f"已按内置示例树建好测试任务「{created.name}」")
            st.rerun()
    st.caption("示例树 = 21 个节点（设备／监测对象／通道／指标），只在你点这个按钮时才建")
    st.stop()

with store_ctx.open_session() as session:
    nodes = tree.list_nodes(session, task.id)
    snapshot = tree.tree_snapshot(session, task.id)
    health = node_health(session, task.id) if snapshot is not None else {}

counts = {key: 0 for key in NODE_TYPE_TEXT}
for row in nodes:
    counts[row.node_type] += 1

page_header(
    "健康树配置",
    "层级：任务 → 设备 → 监测对象 → 通道 → 监测指标；数据来自 task_node 表",
    badge=f"设备 {counts['device']} · 通道 {counts['channel']}",
    demo=False,
)

show_flash()

kpi_row(
    [
        ("设备", str(counts["device"]), None),
        ("监测对象", str(counts["component"]), None),
        ("通道", str(counts["channel"]), None),
        ("监测指标", str(counts["metric"]), f"共 {len(nodes)} 个节点"),
        ("任务", task.code, "健康状态待指标计算后显示"),
    ]
)

# 低频功能用开关而不是 st.expander：Streamlit 的 expander 即使折叠也会把里面的控件
# 全部执行一遍（导出模板还要查库），每点一次树就白算一次（2026-11-05 嫌卡）。
if st.toggle("⚙ 批量工具：模板导入导出（低频操作，用时再打开）", value=False, key="ht_tools_open"):
    # ---------------------------------------------------------------- 模板导入 / 导出
    # 一行三格等宽：导入模板 → 导出模板 → 包含阈值配置（现场定的顺序与均分）
    col_import, col_export, col_thresh = st.columns(3, vertical_alignment="bottom")

    if snapshot is not None:
        include_thresholds = col_thresh.checkbox(
            "包含阈值配置",
            value=True,
            key="tpl_with_thresholds",
            width="stretch",
            help="勾选后模板带 thresholds 区段（按「节点路径 + 指标码」寻址）；"
            "不勾则只有树与指标参数",
        )
        with store_ctx.open_session() as session:
            payload = tree.export_template(
                session, task.id, include_thresholds=bool(include_thresholds)
            )
        col_export.download_button(
            "⬇ 导出模板",
            data=json.dumps(payload, ensure_ascii=False, indent=2),
            file_name=f"{task.code}-health-tree.json",
            mime="application/json",
            width="stretch",
            key="tpl_download",
        )
    else:
        col_thresh.caption("包含阈值配置")
        col_export.button("⬇ 导出模板", disabled=True, width="stretch", key="tpl_download_disabled")

    THRESHOLD_STRATEGIES = {
        "覆盖（默认）": "overwrite",
        "只补缺失": "fill_missing",
        "跳过阈值": "skip",
    }

    PARAM_STRATEGIES = {
        "按生效值搬运（默认）": "override",
        "只搬固化项": "locked_only",
        "只搬覆盖差异": "follow_default",
    }

    with col_import.popover("⬆ 导入模板", width="stretch"):
        hint(
            "上传 JSON → 先预览（树节点 / 指标绑定 / 阈值条数 + 未匹配台账 + 参数差异）"
            "→ 选范围与策略 → 应用"
        )

        upload = st.file_uploader("选择模板文件", type=["json"], key="tpl_upload")
        if upload is not None:
            try:
                candidate = json.loads(upload.getvalue().decode("utf-8", "ignore"))
                with store_ctx.open_session() as session:
                    preview = tree.preview_import(session, task.id, candidate)
            except json.JSONDecodeError as exc:
                ui.error(f"不是合法 JSON：{exc}")
            except TreeError as exc:
                ui.error(f"模板校验未通过：{exc}")
            else:
                if preview.legacy:
                    ui.warning(preview.notice, icon="⚠️")
                else:
                    st.success("结构校验通过（v2）")
                counts_text = "　".join(
                    f"{NODE_TYPE_TEXT[key]} {preview.node_counts[key]}"
                    for key in NODE_TYPE_TEXT
                    if preview.node_counts.get(key)
                )
                st.caption(f"树节点 {preview.node_total} 个：{counts_text}")
                if not preview.legacy:
                    st.caption(
                        f"指标绑定 {preview.metric_bindings} 个　阈值 {preview.threshold_rows} 条"
                        f"　来自任务：{preview.task_name}"
                    )
                    if not preview.has_snapshot:
                        ui.warning(preview.notice, icon="⚠️")
                    elif preview.param_diff_count:
                        ui.warning(
                            f"{preview.param_diff_count} 个指标参数与目标任务默认不同"
                            f"（涉及 {preview.param_diff_nodes} 个指标节点）：",
                            icon="⚠️",
                        )
                        with st.expander("展开参数差异（目标默认 → 模板生效值）", expanded=False):
                            for item in preview.param_diffs:
                                st.caption(f"· {item.text}")
                    else:
                        st.caption("参数与目标任务默认一致，导入不会改变生效值")
                if preview.blocked:
                    ui.error(
                        f"未匹配台账 {len(preview.unmatched)} 处，导入已阻止（不猜、不错挂）："
                    )
                    for item in preview.unmatched:
                        st.caption(f"· {item.text}")
                elif preview.legacy:
                    if st.checkbox("确认用该旧版模板替换健康树", key="tpl_confirm_v1"):
                        if st.button("应用旧版模板", key="tpl_apply_v1"):
                            try:
                                with store_ctx.open_session() as session:
                                    report = tree.import_template(session, task.id, candidate)
                            except TreeError as exc:
                                ui.error(f"模板导入失败：{exc}")
                            else:
                                flash(f"已用旧版模板替换健康树；{report.notice}", icon="⚠️")
                                st.session_state.pop("ht_selected", None)
                                st.rerun()
                else:
                    c_tree, c_par, c_thr = st.columns(3)
                    scope_tree = c_tree.checkbox(
                        "树结构",
                        value=True,
                        key="tpl_scope_tree",
                        help="整树替换当前任务的健康树；不勾=只把参数/阈值同步到现有同名节点",
                    )
                    scope_params = c_par.checkbox("指标参数", value=True, key="tpl_scope_params")
                    scope_thr = c_thr.checkbox("阈值", value=True, key="tpl_scope_thr")
                    strategy_label = st.radio(
                        "阈值策略",
                        list(THRESHOLD_STRATEGIES),
                        index=0,
                        key="tpl_thr_strategy",
                        horizontal=True,
                        disabled=not scope_thr,
                    )
                    param_strategy_label = st.radio(
                        "参数策略",
                        list(PARAM_STRATEGIES),
                        index=0,
                        key="tpl_param_strategy",
                        horizontal=True,
                        disabled=not scope_params or not preview.has_snapshot,
                        help="按生效值搬运=用模板里的生效值覆盖（避免跨任务默认不同导致漂移）；"
                        "只搬固化项=仅固化节点按生效值；只搬覆盖差异=旧行为",
                    )
                    if st.checkbox("确认按上述范围应用该模板", key="tpl_confirm_v2"):
                        if st.button("应用模板", key="tpl_apply_v2"):
                            options = tree_template.ImportOptions(
                                structure=bool(scope_tree),
                                params=bool(scope_params),
                                thresholds=bool(scope_thr),
                                threshold_strategy=THRESHOLD_STRATEGIES[strategy_label],
                                param_strategy=PARAM_STRATEGIES[param_strategy_label],
                            )
                            try:
                                with store_ctx.open_session() as session:
                                    report = tree.import_template(
                                        session, task.id, candidate, options
                                    )
                            except TreeError as exc:
                                ui.error(f"模板导入失败：{exc}")
                            else:
                                flash(_report_text(report))
                                st.session_state.pop("ht_selected", None)
                                st.rerun()

        st.divider()
        if st.checkbox("确认用内置示例树替换", key="tpl_reset_confirm"):
            if st.button("恢复内置示例树", key="tpl_reset"):
                with store_ctx.open_session() as session:
                    tree.import_template(
                        session, task.id, bootstrap.bootstrap_example_tree_payload()
                    )
                flash("已恢复内置示例树")
                st.session_state.pop("ht_selected", None)
                st.rerun()

    hint(
        "模板 v2：节点层级 + 属性 + 台账业务键（对象名称/部位、通道号、指标码）"
        "+ 指标参数（含生效值快照）；勾「包含阈值配置」才带阈值区段"
    )

if snapshot is None:
    st.info(
        "当前任务还没有健康树。展开上方「⚙ 批量工具」导入模板，或在那里恢复内置示例树。",
        icon="🌳",
    )
    st.stop()

# ---------------------------------------------------------------- 树 + 详情
left, right = st.columns([0.44, 0.56])

with left:
    section("节点树", "＋/－ 展开折叠；点名称选中后右侧编辑")
    _apply_health(snapshot, health)
    selected_id = tree_ui.render_tree(
        snapshot,
        state_key="ht_selected",
        box_key="ht_treebox",
        caption="点节点名称选中；设备前复选框只影响呈现",
        fragment=True,
    )

by_id = {row.id: row for row in nodes}
try:
    picked = int(selected_id)
except (TypeError, ValueError):
    picked = None
node = by_id.get(picked) or by_id[int(snapshot["id"])]
trail = [by_id[int(part)] for part in node.path.split("/") if int(part) in by_id]

with right:
    child_total = sum(1 for row in nodes if row.parent_id == node.id)
    section(
        f"{TYPE_ICON.get(node.node_type, '·')} {node.name}",
        f"类型：{node.type_text}　ID：{node.id}　子节点 {child_total} 个",
    )
    crumbs = " › ".join(item.name for item in trail)
    st.markdown(
        "<div style='font-family:Consolas,monospace;font-size:12px;color:#cfd8e3;"
        "background:#16202b;border:1px solid #2a3441;border-radius:3px;"
        f"padding:3px 8px'>[{crumbs}]</div>",
        unsafe_allow_html=True,
    )

    tab_labels = ["节点属性"]
    if node.node_type == "channel":
        tab_labels.append("监测指标")
    elif node.node_type == "metric":
        tab_labels.append("指标参数")
    if node.node_type != "metric":
        tab_labels.append("子节点")
    tab_labels.append("改名与删除")
    # key：刷新后保持原来选中的页签（否则装配完会跳回「节点属性」，看着像画面被弹走）；
    # on_change：切页签时撤掉删除待确认态，别让红按钮一直挂着
    tabs = dict(
        zip(
            tab_labels,
            st.tabs(
                tab_labels,
                key=f"ht_tabs_{node.id}",
                on_change=_reset_node_delete_arm,
                args=(node.id,),
            ),
            strict=True,
        )
    )
    tab_attr = tabs["节点属性"]
    tab_ops = tabs["改名与删除"]
    tab_children = tabs.get("子节点")
    tab_metrics = tabs.get("监测指标")
    tab_metric_params = tabs.get("指标参数")

    with tab_attr:
        if node.ref_type and _ref_missing(task, node):
            ui.warning(
                f"台账里找不到这条记录（{node.ref_type} #{node.ref_id}）：可能是台账行被删了。"
                "可以重新装配，或清除引用把它变成手工节点。"
            )
            if st.button("清除台账引用（保留节点）", key=f"ht_clear_ref_{node.id}"):
                with store_ctx.open_session() as session:
                    tree.update_node(session, node.id, clear_ref=True)
                flash(f"已清除引用：{node.name}")
                st.rerun()
        if node.ref_type:
            with store_ctx.open_session() as session:
                pools = {
                    "monitor_object": {
                        r.id: r.name for r in object_registry.list_objects(session, task.id)
                    },
                    "channel_reg": {
                        r.id: r.code for r in channel_reg.list_channels(session, task.id)
                    },
                    "metric_def": {r.id: r.code for r in metric_def.list_metrics(session, task.id)},
                }
            pool = pools.get(node.ref_type, {})
            if node.ref_id in pool:
                st.caption(f"🔗 台账引用：{node.ref_type} #{node.ref_id}（{pool[node.ref_id]}）")
            else:
                ui.warning(
                    f"⚠️ 悬空引用：{node.ref_type} #{node.ref_id} 在台账中已删除或停用，"
                    "请从台账恢复，或删除该节点后重新装配"
                )
        elif node.node_type in ("component", "channel", "metric"):
            st.caption("未关联台账（早期手工录入的节点，可删除后从台账重新装配）")

        if node.node_type == "component" and node.ref_type == "monitor_object" and node.ref_id:
            with store_ctx.open_session() as session:
                objs = {row.id: row for row in object_registry.list_objects(session, task.id)}
            target = objs.get(node.ref_id)
            if target is not None:
                section("额定转速", "属于这台监测对象；阶次类指标用它换算出特征频率")
                c_rpm, c_save = st.columns([0.7, 0.3], vertical_alignment="bottom")
                rated_input = c_rpm.number_input(
                    "额定转速（rpm）",
                    value=float(target.rated_rpm or 0.0),
                    min_value=0.0,
                    step=10.0,
                    key=f"ht_rated_{node.id}",
                    help="0 表示未设置；批次实测转速优先于它",
                )
                if c_save.button(
                    "保存额定转速", key=f"ht_rated_save_{node.id}", use_container_width=True
                ):
                    with store_ctx.open_session() as session:
                        object_registry.set_rated_rpm(
                            session, node.ref_id, float(rated_input) or None
                        )
                    flash("已保存额定转速" if float(rated_input) > 0 else "已清空额定转速")
                    st.rerun()

        if node.node_type in ("component", "channel"):
            render_freq_block(task, node)

        override_keys = {
            key for key in node.attrs if str(key).startswith(component_freq.FREQ_ATTR_PREFIX)
        }
        plain_attrs = {key: value for key, value in node.attrs.items() if key not in override_keys}
        if plain_attrs:
            with st.form(f"attrs_{node.id}"):
                inputs = {}
                cols = st.columns(2)
                for i, (key, value) in enumerate(plain_attrs.items()):
                    inputs[key] = cols[i % 2].text_input(key, value=str(value))
                if st.form_submit_button("保存属性"):
                    merged = {**{key: node.attrs[key] for key in override_keys}, **inputs}
                    with store_ctx.open_session() as session:
                        tree.update_node(session, node.id, attrs=merged)
                    flash(f"已保存：{node.name} 的属性")
                    st.rerun()
        else:
            st.caption("该节点还没有属性")

        if node.node_type == "channel":
            with store_ctx.open_session() as session:
                sensor = sensors.sensor_of_node(session, node.id)
                ledger = {c.id: c for c in channel_reg.list_channels(session, task.id)}
            owner = ledger.get(node.ref_id) if node.ref_type == "channel_reg" else None
            owner_text = (
                owner.object_name
                if owner is not None and not owner.is_free
                else "自由（不归属任何监测对象；到「传感器」页改绑）"
            )
            section("通道属性", "传感器信息在「传感器」页维护，这里只读展示同一份数据")
            st.caption(f"归属监测对象：{owner_text}")
            if sensor is None:
                st.caption("该通道还没有挂传感器：下面可以直接录入，也可以去「传感器」页管理")
                with st.expander("＋ 录入传感器（直接挂到本通道）", expanded=False):
                    with st.form(f"quick_sensor_{node.id}"):
                        col_a, col_b = st.columns(2)
                        model_in = col_a.text_input("型号", placeholder="如：IEPE 500mV/g")
                        sens_in = col_b.text_input("灵敏度", placeholder="如：500 mV/g")
                        col_c, col_d = st.columns(2)
                        range_in = col_c.text_input("量程（g）", placeholder="如：±10")
                        mount_in = col_d.text_input("安装方式", placeholder="如：磁吸")
                        cal_in = st.text_input("标定日期", placeholder="如：2026-09-20")
                        if st.form_submit_button("录入并挂上"):
                            try:
                                with store_ctx.open_session() as session:
                                    sensors.create_sensor(
                                        session,
                                        task.id,
                                        node.id,
                                        model=model_in,
                                        sensitivity=sens_in,
                                        range_g=range_in,
                                        mounting=mount_in,
                                        calibrated_on=cal_in,
                                    )
                            except SensorError as exc:
                                ui.error(str(exc))
                            else:
                                flash(f"已给 {node.name} 录入传感器")
                                st.rerun()
            else:
                kv_block(
                    {
                        "传感器型号": sensor.model or "—",
                        "灵敏度": sensor.sensitivity or "—",
                        "量程": sensor.range_g or "—",
                        "安装方式": sensor.mounting or "—",
                        "标定日期": sensor.calibrated_on or "—",
                        "状态": sensor.status,
                    }
                )

    if tab_metrics is not None:
        with tab_metrics:
            metric_params_ui.render(task.id, node)

    if tab_metric_params is not None:
        with tab_metric_params:
            metric_params_ui.render_metric_node(task.id, node)

    if tab_children is not None:
        with tab_children:
            kids = [row for row in nodes if row.parent_id == node.id]
            if kids:
                for child in kids:
                    st.markdown(
                        f"{TYPE_ICON.get(child.node_type, '·')} **{child.name}**"
                        f"　{child.type_text}　ID {child.id}"
                    )
            else:
                st.caption("暂无子节点")

            child_type = CHILD_OF.get(node.node_type)
            if child_type:
                # 加完子节点后页面会重跑：展开状态与滚动位置要保住，别把人弹回顶部
                open_key = f"ht_addopen_{node.id}"
                with st.expander(
                    f"＋ 新增{NODE_TYPE_TEXT[child_type]}",
                    expanded=bool(st.session_state.get(open_key, False)),
                ):
                    # 台账里已配好的选项（只选不输）
                    with store_ctx.open_session() as session:
                        obj_pool = object_registry.list_objects(session, task.id)
                        ch_pool = channel_reg.list_channels(session, task.id)
                        sensor_models = sensors.models_by_channel(session, task.id)
                        mt_pool = metric_def.list_metrics(session, task.id)
                    used_names = {row.name for row in nodes}
                    ref_type, ref_id = "", None

                    if child_type == "channel":
                        _render_add_channel(node, nodes, ch_pool, sensor_models)

                    if child_type != "channel":
                        with st.form(f"add_{node.id}"):
                            if child_type == "component":
                                options = [obj for obj in obj_pool if obj.name not in used_names]
                                if not options:
                                    st.caption("台账里没有可用的监测对象，先到「监测对象」页新增")
                                    chosen = None
                                else:
                                    label = st.selectbox(
                                        "监测对象（来自台账）",
                                        [f"{o.name}（{o.obj_type}）" for o in options],
                                        key=f"po_{node.id}",
                                    )
                                    chosen = options[
                                        [f"{o.name}（{o.obj_type}）" for o in options].index(label)
                                    ]
                                new_name = chosen.name if chosen else ""
                                new_attrs = (
                                    {
                                        "类型": chosen.obj_type,
                                        **({"部位": chosen.part} if chosen.part else {}),
                                    }
                                    if chosen
                                    else {}
                                )
                                ref_type, ref_id = (
                                    ("monitor_object", chosen.id) if chosen else ("", None)
                                )
                            elif child_type == "metric":
                                if not mt_pool:
                                    st.caption(
                                        "台账里没有指标，先到「指标配置」页新增或补内置三指标"
                                    )
                                    chosen_m = None
                                else:
                                    label = st.selectbox(
                                        "指标（来自台账）",
                                        [
                                            f"{m.code}（{m.unit}）" if m.unit else m.code
                                            for m in mt_pool
                                        ],
                                        key=f"pm_{node.id}",
                                    )
                                    chosen_m = mt_pool[
                                        [f"{m.code}（{m.unit}）" for m in mt_pool].index(label)
                                    ]
                                new_name = chosen_m.code if chosen_m else ""
                                new_attrs = (
                                    {"code": chosen_m.code, "单位": chosen_m.unit}
                                    if chosen_m
                                    else {}
                                )
                                ref_type, ref_id = (
                                    ("metric_def", chosen_m.id) if chosen_m else ("", None)
                                )
                            else:
                                new_name = st.text_input("名称", placeholder="如：卷绕产线")
                                station = st.text_input("工位", placeholder="如：WL-1")
                                new_attrs = {"工位": station} if station else {}

                            if st.form_submit_button("添加到树上"):
                                try:
                                    with store_ctx.open_session() as session:
                                        created = tree.create_node(
                                            session,
                                            task.id,
                                            child_type,
                                            new_name,
                                            parent_id=node.id,
                                            attrs=new_attrs,
                                            ref_type=ref_type,
                                            ref_id=ref_id,
                                        )
                                except TreeError as exc:
                                    ui.error(str(exc))
                                else:
                                    note = ""
                                    if child_type == "metric" and new_attrs.get("code"):
                                        try:
                                            with store_ctx.open_session() as session:
                                                metric_assign.assign_metric(
                                                    session, task.id, created.id, new_attrs["code"]
                                                )
                                        except MetricAssignError as exc:
                                            note = f"（参数卡未建：{exc}）"
                                    st.session_state[f"ht_addopen_{node.id}"] = True
                                    flash(f"已添加：{created.name}（{created.type_text}）{note}")
                                    st.rerun()
            if child_type == "component":
                with st.expander("＋ 就地新建监测对象（不用去台账页）", expanded=False):
                    with st.form(f"quick_obj_{node.id}"):
                        col_a, col_b = st.columns(2)
                        obj_name = col_a.text_input("对象名称", placeholder="如：入片牵引电机")
                        obj_type = col_b.selectbox(
                            "对象类型", OBJECT_TYPES, key=f"qo_type_{node.id}"
                        )
                        col_c, col_d = st.columns(2)
                        obj_part = col_c.text_input("部位", placeholder="如：驱动端")
                        obj_rpm = col_d.number_input(
                            "额定转速（rpm）", value=0.0, min_value=0.0, step=10.0
                        )
                        if st.form_submit_button("新建并装配"):
                            try:
                                with store_ctx.open_session() as session:
                                    obj = object_registry.create_object(
                                        session, task.id, obj_name, obj_type, part=obj_part
                                    )
                                    if float(obj_rpm) > 0:
                                        object_registry.set_rated_rpm(
                                            session, obj.id, float(obj_rpm)
                                        )
                                    attrs = {"类型": obj.obj_type}
                                    if obj.part:
                                        attrs["部位"] = obj.part
                                    tree.create_node(
                                        session,
                                        task.id,
                                        "component",
                                        obj.name,
                                        parent_id=node.id,
                                        attrs=attrs,
                                        ref_type="monitor_object",
                                        ref_id=obj.id,
                                    )
                            except (ObjectRegistryError, TreeError) as exc:
                                ui.error(str(exc))
                            else:
                                note = (
                                    f"额定转速 {float(obj_rpm):g} rpm"
                                    if float(obj_rpm) > 0
                                    else "额定转速未填"
                                )
                                flash(f"已新建并装配：{obj.name}（{note}）")
                                st.rerun()
            else:
                st.caption("监测指标是最末级，指标下面不再挂节点；阈值请去「阈值设置」配置")

    with tab_ops:
        with st.form(f"rename_{node.id}"):
            renamed = st.text_input("节点名称", value=node.name)
            if st.form_submit_button("改名"):
                try:
                    with store_ctx.open_session() as session:
                        tree.update_node(session, node.id, name=renamed)
                except TreeError as exc:
                    ui.error(str(exc))
                else:
                    flash(f"已改名：{renamed}")
                    st.rerun()

        st.divider()
        descendants = 0
        for row in nodes:
            if row.path.startswith(f"{node.path}/"):
                descendants += 1
        st.caption(f"删除将级联软删该节点及其下 {descendants} 个子节点，可在回收站恢复")
        # 删除二合一（与本页其它删除、监测对象 / 传感器 / 阈值页一致）：
        # 第一次变红「确认删除该节点及子树？」，第二次才真删；armed 按节点 id 存
        arm_key = f"del_node_armed_{node.id}"
        armed = bool(st.session_state.get(arm_key))
        if armed:
            st.markdown(danger_button_css(f"del_{node.id}"), unsafe_allow_html=True)
        if st.button(
            "确认删除该节点及子树？" if armed else "删除节点",
            key=f"del_{node.id}",
            help="点两次删除：第一次进入待确认，第二次执行",
        ):
            if not armed:
                st.session_state[arm_key] = True
                st.rerun()
            with store_ctx.open_session() as session:
                removed = tree.soft_delete_node(session, node.id)
            total = sum(removed.values())
            st.session_state.pop(arm_key, None)
            flash(f"已删除 {total} 个节点（含子树）", icon="🗑️")
            st.session_state.pop("ht_selected", None)
            st.rerun()
