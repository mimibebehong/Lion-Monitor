"""趋势分析（方案 9.10）：左侧健康树 + 右侧「趋势 / 健康评级」两页签。

- 树种不含阈值节点，节点状态来自真实指标值 vs 继承阈值（features.health）
- 趋势：默认最近 30 天；图上超过 2000 点按 min/max 保峰抽稀（features.trend_view，Q6），
  导出 CSV 始终是全量原始点
- 选中指标直接出曲线；选中通道/对象/设备/任务则列出其下全部指标（最多 4 个，单位不同自动分图）
- 健康评级：节点评级表（状态/最新值/超限条数）+ 超限记录
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import tree_ui  # noqa: E402
import trend_rows  # noqa: E402
from ui import danger_button_css, flash, kpi_row, page_header, section, show_flash  # noqa: E402

from lion_monitor.features import recompute, trend_view  # noqa: E402
from lion_monitor.features.health import node_health, point_level  # noqa: E402
from lion_monitor.features.metrics import METRIC_UNITS  # noqa: E402
from lion_monitor.features.series import metric_series  # noqa: E402
from lion_monitor.store import limits, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402

#: 旧参数值（``stale=1``）在趋势图上的颜色：灰色空心点，与彩色实心点区分（S4A）
STALE_COLOR = "#8b949e"
STALE_TRACE = "旧参数（待重算）"

TASK = store_ctx.current_task()
if TASK is None:
    page_header("趋势分析", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

page_header(
    "趋势分析",
    "左选节点 → 右侧看趋势 / 评级（默认近 30 天）",
    badge=TASK.name,
    demo=False,
)
show_flash()


#: 指标节点 id → {通道号, 指标码}，渲染前一次性算好
METRIC_INFO: dict[int, dict] = {}


def _annotate(
    node: dict, health: dict, channel: str | None = None, channel_id: int | None = None
) -> None:
    """把真实健康状态注入树节点（tree_ui 优先读 node['state']），并登记指标节点归属通道。

    ``channel_id`` 是**通道节点** id —— 指标值按节点存，取数时要它（P2 / S1）。
    """
    item = health.get(node["id"])
    node["state"] = item.state if item else "unset"
    node["children"] = [child for child in node.get("children", []) if child["type"] != "threshold"]
    here = node["name"] if node["type"] == "channel" else channel
    here_id = node["id"] if node["type"] == "channel" else channel_id
    if node["type"] == "metric" and here:
        METRIC_INFO[node["id"]] = {
            "channel": here,
            "code": (node.get("attrs") or {}).get("code") or node["name"],
            "channel_node_id": here_id,
        }
    for child in node["children"]:
        _annotate(child, health, here, here_id)


def _walk_snapshot(node: dict):
    """深度遍历嵌套树快照。"""
    yield node
    for child in node.get("children", []):
        yield from _walk_snapshot(child)


def _metric_targets(node: dict) -> list[dict]:
    """选中节点可画图的指标：指标节点出自己，通道/对象/设备/任务出子树全部。"""
    if node["type"] == "metric":
        info = METRIC_INFO.get(node["id"])
        return [{**info, "node_id": node["id"]}] if info else []
    out: list[dict] = []
    for item in _walk_snapshot(node):
        if item["type"] != "metric":
            continue
        info = METRIC_INFO.get(item["id"])
        if info:
            out.append({**info, "node_id": item["id"]})
    return out


#: 节点评级表样式：树形前缀用等宽字体对齐，数字列用等宽数字，「超限条数」居中（马哥 2026-11-03）
RANK_TABLE_CSS = """<style>
.rank-table { width:100%; border-collapse:collapse; font-size:13px; }
.rank-table th { text-align:left; font-weight:500; font-size:12px; color:#8b949e;
  padding:6px 10px; border-bottom:1px solid #26303c; white-space:nowrap; }
.rank-table td { padding:5px 10px; border-bottom:1px solid #1b232d; color:#d5dde7; }
.rank-table tbody tr:hover td { background:#161d26; }
.rank-table .pre { font-family:"Consolas","DejaVu Sans Mono",monospace; color:#4a5666;
  white-space:pre; letter-spacing:0; }
.rank-table .icon { margin-right:4px; }
.rank-table td.num, .rank-table th.num { text-align:right; font-variant-numeric:tabular-nums; }
.rank-table td.cnt, .rank-table th.cnt { text-align:center; font-variant-numeric:tabular-nums; }
.rank-table td.unit { color:#8b949e; }
.rank-table .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
  margin-right:6px; vertical-align:middle; }
</style>"""


def rank_table_html(snapshot: dict, health: dict) -> str:
    """节点评级表：节点列用 ├─/└─ 树形前缀缩进，状态用色点，「超限条数」居中。

    用 HTML 表格而不是 ``st.dataframe``：dataframe 是 canvas 渲染，
    列对齐只能「数值靠右/文本靠左」，做不到居中，也做不出树形缩进的精致感。
    """
    rows: list[str] = []

    def walk(node: dict, prefixes: list[bool], is_last: bool, depth: int) -> None:
        item = health.get(node["id"])
        state = item.state if item else "unset"
        latest = "—" if item is None or item.latest_value is None else f"{item.latest_value:.4f}"
        unit = (item.unit if item else "") or ""
        over = item.over_count if item else 0
        # 复查 v2 问题 4：没有可判级的有效点（全待重算）→ unset + 「待重算」，不判正常
        pending = (
            " <span class='unit'>· 待重算</span>"
            if item is not None and item.needs_recompute
            else ""
        )
        prefix = "".join("　 " if last else "│ " for last in prefixes)
        branch = "" if depth == 0 else ("└─ " if is_last else "├─ ")
        icon = tree_ui.TYPE_ICON.get(node["type"], "")
        rows.append(
            "<tr>"
            f"<td><span class='pre'>{prefix}{branch}</span>"
            f"<span class='icon'>{icon}</span>{html.escape(str(node['name']))}</td>"
            f"<td class='unit'>{tree_ui.TYPE_TEXT.get(node['type'], node['type'])}</td>"
            f"<td><span class='dot' style='background:{tree_ui.STATE_COLOR[state]}'></span>"
            f"{tree_ui.STATE_TEXT[state]}{pending}</td>"
            f"<td class='num'>{latest}</td>"
            f"<td class='unit'>{html.escape(unit)}</td>"
            f"<td class='cnt'>{over}</td>"
            "</tr>"
        )
        kids = node.get("children", [])
        for index, child in enumerate(kids):
            walk(child, [*prefixes, is_last] if depth else [], index == len(kids) - 1, depth + 1)

    walk(snapshot, [], True, 0)
    head = (
        "<tr><th>节点</th><th>类型</th><th>状态</th>"
        "<th class='num'>最新值</th><th>单位</th><th class='cnt'>超限条数</th></tr>"
    )
    return (
        RANK_TABLE_CSS
        + "<table class='rank-table'><thead>"
        + head
        + "</thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _recompute_block(
    pairs: list[tuple[int, str | None]],
    key: str,
    note: str = "",
    export: tuple[str, bytes] | None = None,
) -> None:
    """重算入口（趋势页按节点+指标、评级页按节点）：预览「将重算 N 条、预计耗时」→ 确认后重算。

    ``pairs`` = [(通道节点 id, 指标码 or None)]；``None`` 表示该节点树上挂的全部指标。
    预览与重算都走 ``features.recompute``，页面只负责按钮与提示。

    ``export`` = (文件名, CSV 字节)：给了就与「重算本节点」**同一行**渲染（马哥 2026-11-04），
    导出在左、重算在右，左列仍留给提示/进度条。
    """
    with store_ctx.open_session() as session:
        plans = [
            recompute.recompute_plan(session, TASK.id, node_id, code) for node_id, code in pairs
        ]
    # 马哥 2026-11-03：不再显示「将重算 N 条、预计 X 秒」，改成点击后同一行左侧走真实进度条
    has_work = any(plan.point_estimate for plan in plans)
    blocked = [text for plan in plans for text in plan.problems]

    armed_key = f"{key}_armed"
    armed = bool(st.session_state.get(armed_key))
    if export is None:
        left_col, right_col = st.columns([0.6, 0.4], vertical_alignment="center")
        export_col = None
    else:
        # 马哥 2026-11-04：导出按钮与「重算本节点」放同一行（导出在左、重算在右）
        left_col, export_col, right_col = st.columns(
            [0.40, 0.34, 0.26], vertical_alignment="center"
        )
    if blocked:
        left_col.caption(f"无法重算：{blocked[0]}")
    if export_col is not None and export is not None:
        file_name, payload = export
        with export_col:
            st.download_button(
                "导出逐点数据 CSV",
                payload,
                file_name=file_name,
                mime="text/csv",
                key=f"{key}_export",
                width="stretch",
            )
    # 二次点击确认（马哥 2026-11-03）：与任务页删除同一套交互，不再用勾选框
    with right_col, st.container(key=f"{key}_arm"):
        if armed:
            st.markdown(danger_button_css(f"{key}_btn"), unsafe_allow_html=True)
        clicked = st.button(
            "确认重算？" if armed else "重算本节点",
            key=f"{key}_btn",
            width="stretch",
            disabled=not has_work or bool(blocked),
            help="点两次：第一次进入待确认，第二次执行（旧值覆盖、作废的旧点位软删）",
        )
    if clicked:
        if not armed:
            st.session_state[armed_key] = True
            st.rerun()
        st.session_state.pop(armed_key, None)
        done = retired = 0
        skipped: list[str] = []
        total = max(1, len(pairs))
        bar = left_col.progress(0.0, text="重算中…")
        with store_ctx.open_session() as session:
            for index, (node_id, code) in enumerate(pairs):

                def report(fraction: float, text: str = "", _i: int = index) -> None:
                    bar.progress(min((_i + fraction) / total, 1.0), text=text or "重算中…")

                outcome = recompute.recompute_node(session, TASK.id, node_id, code, progress=report)
                done += outcome.recomputed
                retired += outcome.retired
                skipped.extend(outcome.problems)
                bar.progress((index + 1) / total, text=f"已重算 {done} 条")
        flash(
            f"重算完成：覆盖 {done} 条值、作废旧点位 {retired} 条"
            + (f"；{len(skipped)} 处跳过（{skipped[0]}）" if skipped else ""),
            icon="🔁",
            tone="warning" if skipped else "success",
        )
        st.rerun()


with store_ctx.open_session() as session:
    snapshot = tree.tree_snapshot(session, TASK.id)
    health = node_health(session, TASK.id)
    all_nodes = tree.list_nodes(session, TASK.id)

if snapshot is None:
    section("趋势分析")
    st.info("健康树还是空的：先去「健康树配置」装配设备 / 监测对象 / 通道 / 指标", icon="ℹ️")
    st.stop()

_annotate(snapshot, health)

_BY_ID = {node.id: node for node in all_nodes}
_PARENT = {node.id: node.parent_id for node in all_nodes}


def _object_of(node_id: int) -> int | None:
    """沿父链找到所属监测对象（component）节点 id。"""
    cursor: int | None = node_id
    while cursor is not None:
        node = _BY_ID.get(cursor)
        if node is None:
            return None
        if node.node_type == "component":
            return node.id
        cursor = _PARENT.get(cursor)
    return None


#: 通道名 → 本任务下的通道节点个数（>1 = 同一采集通道挂了多个监测对象，Q1 允许）
_CHANNEL_COUNT: dict[str, int] = {}
for _node in all_nodes:
    if _node.node_type == "channel":
        _CHANNEL_COUNT[_node.name] = _CHANNEL_COUNT.get(_node.name, 0) + 1

#: 监测对象 id → 名称；通道节点 id → 所属监测对象名（CSV「监测对象」列按行归属填）
_OBJECT_NAME = {node.id: node.name for node in all_nodes if node.node_type == "component"}
OBJECT_OF_CHANNEL = {
    node.id: _OBJECT_NAME.get(_object_of(node.id) or -1, "")
    for node in all_nodes
    if node.node_type == "channel"
}


left, right = st.columns([0.34, 0.66], gap="medium")

with left:
    picked = tree_ui.render_tree(
        snapshot,
        state_key="trend_tree",
        box_key="trend_box",
        device_checks=False,
        caption="",
        caption_help="＋/－ 展开折叠；点节点名称选中",
        hide_types={"threshold"},
        fragment=True,
    )

picked_id = int(picked) if str(picked).isdigit() else int(snapshot["id"])
picked_node = next((n for n in all_nodes if n.id == picked_id), None)

with right:
    # on_change 才会开「按需渲染」：不选中的页签（健康评级要算全任务超限记录）
    # 不再每次重跑都白算一遍（马哥 2026-11-05 嫌卡）
    tab_trend, tab_rank = st.tabs(
        ["趋势", "健康评级"], key="trend_tabs", on_change="rerun"
    )

    with tab_trend:
        if picked_node is None:
            st.info("先在左侧选中一个节点", icon="ℹ️")
            st.stop()

        # 指标范围限定在「一个监测对象」内：选中设备/任务时先挑对象
        object_nodes = [n for n in all_nodes if n.node_type == "component"]
        if not object_nodes:
            section(f"{picked_node.name} · 趋势")
            st.info("健康树里还没有监测对象：先去「健康树配置」装配", icon="ℹ️")
            st.stop()

        scope_id = _object_of(picked_node.id)
        if scope_id is None:
            names = {o.id: o.name for o in object_nodes}
            scope_id = st.selectbox(
                "监测对象（一次只看一个对象下的指标）",
                list(names),
                format_func=lambda i: names[i],
                key=f"trend_object_{picked_node.id}",
            )
        scope_name = next(o.name for o in object_nodes if o.id == scope_id)

        targets = [
            {
                "channel": info["channel"],
                "code": info["code"],
                "node_id": nid,
                "channel_node_id": info["channel_node_id"],
            }
            for nid, info in METRIC_INFO.items()
            if _object_of(nid) == scope_id
        ]
        if not targets:
            section(f"{scope_name} · 趋势")
            st.info("这个监测对象下还没有指标节点：去「健康树配置」在通道下挂指标", icon="ℹ️")
            st.stop()

        options = [f"{t['channel']} · {t['code']}" for t in targets]
        if picked_node.node_type == "metric":
            default = [
                label
                for t, label in zip(targets, options, strict=True)
                if t["node_id"] == picked_node.id
            ]
            st.caption(f"📐 选中指标节点：{default[0] if default else scope_name}")
        elif picked_node.node_type == "channel":
            default = [
                label
                for t, label in zip(targets, options, strict=True)
                if t["channel"] == picked_node.name
            ]
        else:
            default = list(options)
        chosen = st.multiselect(
            f"{scope_name} 的指标（每个子图一条曲线）",
            options,
            default=default,
            key=f"trend_pick_{picked_node.id}",
        )

        picked_targets = [t for t, label in zip(targets, options, strict=True) if label in chosen]
        if not picked_targets:
            st.info("先选至少一个指标", icon="ℹ️")
            st.stop()

        window_label = st.selectbox(
            "时间窗",
            list(trend_view.WINDOW_CHOICES),
            index=0,
            key="trend_window",
            help="默认最近 30 天；图上超过 2000 点按 min/max 抽稀，导出 CSV 始终是全量原始点",
        )
        since = trend_view.window_start(trend_view.WINDOW_CHOICES[window_label])
        x_mode = (
            st.segmented_control(
                "横坐标",
                list(trend_view.X_MODES),
                default=trend_view.X_MODE_TIME,
                key="trend_x_mode",
                label_visibility="collapsed",
            )
            or trend_view.X_MODE_TIME
        )

        with store_ctx.open_session() as session:
            # 取数一律按节点（P2 / S1）：同一通道号挂了多个对象时各读各的通道节点
            series = {
                target["node_id"]: metric_store.list_values(
                    session,
                    TASK.id,
                    target["channel"],
                    target["code"],
                    node_id=target["channel_node_id"],
                    include_unattributed=_CHANNEL_COUNT.get(target["channel"], 0) <= 1,
                    since=since,
                )
                for target in picked_targets
            }
            limit_map = {
                target["node_id"]: limits.resolve_limit(
                    session, TASK.id, target["node_id"], target["code"]
                )
                for target in picked_targets
            }

        # 全页一行说明（原来每条曲线各一行「📈 CH · code 共 N 点」，多指标时太吵）
        counts = [len(series[target["node_id"]]) for target in picked_targets]
        stale_total_n = sum(
            1 for target in picked_targets for row in series[target["node_id"]] if row.stale
        )
        point_text = (
            f"共 {counts[0]} 个点"
            if len(set(counts)) == 1
            else f"共 {min(counts)}~{max(counts)} 个点"
        )
        plotted_counts = []
        for target in picked_targets:
            rows_of = series[target["node_id"]]
            plotted_counts.append((len(trend_view.chart_points(rows_of)[0]), len(rows_of)))
        summary = f"📈 {len(picked_targets)} 条曲线 · {point_text}"
        if stale_total_n:
            summary += f"，其中 {stale_total_n} 个由旧参数算出"
        thinned = [plotted for plotted, total in plotted_counts if plotted < total]
        if thinned:
            # 抽稀后保留真实点数（原「抽稀到 N 点」的信息不丢）
            where = (
                f"到 {thinned[0]} 点"
                if len(set(thinned)) == 1
                else f"到 {min(thinned)}~{max(thinned)} 点"
            )
            summary += f"；图上按 min/max 抽稀{where}，导出 CSV 为全量"
        st.caption(summary)

        detail: list[dict] = []
        for target in picked_targets:
            code, channel_name = target["code"], target["channel"]
            rows = series[target["node_id"]]
            limit = limit_map[target["node_id"]]
            unit = METRIC_UNITS.get(code, "")
            # 图上按 min/max 抽稀（Q6），导出 CSV 仍用全量 rows —— 二者不混用
            plotted, decimated = trend_view.chart_points(rows)
            stale_n = sum(1 for row in rows if row.stale)  # 全量口径（含抽稀掉的点）
            stale_note = f"，其中 {stale_n} 个由旧参数算出" if stale_n else ""

            fig = go.Figure()
            if plotted:
                axis_x, axis_labels, axis_title = trend_view.axis_points(plotted, x_mode)
                x_of = dict(zip(axis_labels, axis_x, strict=True))
                values = [r.value for r in plotted]
                fresh = [r for r in plotted if not r.stale]
                stale = [r for r in plotted if r.stale]
                # 「测试点数」只是排版：hover 依旧给北京时间（马哥要求信息不变）
                label_of = {
                    trend_view.beijing_moment(r.computed_at).strftime("%Y-%m-%d %H:%M:%S"): label
                    for r, label in zip(plotted, axis_labels, strict=True)
                }
                index_mode = x_mode == trend_view.X_MODE_INDEX
                fig.add_trace(
                    go.Scatter(
                        x=axis_x,
                        y=values,
                        mode="lines",
                        name=f"{channel_name} · {code}",
                        showlegend=False,
                        line={"color": "#4c9be8", "width": 2},
                        customdata=axis_labels if index_mode else None,
                        hovertemplate=(
                            "%{customdata}<br>%{y:.4f} " + unit + "<extra></extra>"
                            if index_mode
                            else "%{x|%Y-%m-%d %H:%M:%S}<br>%{y:.4f} " + unit + "<extra></extra>"
                        ),
                    )
                )
                if fresh:
                    fig.add_trace(
                        go.Scatter(
                            x=[
                                x_of[
                                    trend_view.beijing_moment(r.computed_at).strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    )
                                ]
                                for r in fresh
                            ],
                            y=[r.value for r in fresh],
                            mode="markers",
                            name=f"{code} · 正常点",
                            showlegend=False,
                            marker={
                                "size": 9,
                                "color": [
                                    tree_ui.STATE_COLOR[point_level(r.value, limit)] for r in fresh
                                ],
                            },
                            customdata=[
                                trend_view.beijing_moment(r.computed_at).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                                for r in fresh
                            ]
                            if index_mode
                            else None,
                            hovertemplate=(
                                "%{customdata}<br>%{y:.4f} " + unit + "<extra></extra>"
                                if index_mode
                                else "%{x|%Y-%m-%d %H:%M:%S}<br>%{y:.4f} "
                                + unit
                                + "<extra></extra>"
                            ),
                        )
                    )
                if stale:
                    # 旧参数算出的点：灰色空心，图例点名告警（S4A：不能与正常点混在一起看）
                    fig.add_trace(
                        go.Scatter(
                            x=[
                                x_of[
                                    trend_view.beijing_moment(r.computed_at).strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    )
                                ]
                                for r in stale
                            ],
                            y=[r.value for r in stale],
                            mode="markers",
                            name=STALE_TRACE,
                            showlegend=True,
                            marker={
                                "size": 9,
                                "symbol": "circle-open",
                                "color": STALE_COLOR,
                                "line": {"color": STALE_COLOR, "width": 2},
                            },
                            customdata=[
                                trend_view.beijing_moment(r.computed_at).strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                                for r in stale
                            ]
                            if index_mode
                            else None,
                            hovertemplate=(
                                "%{customdata}<br>%{y:.4f} "
                                + unit
                                + "（旧参数算出，未重算）<extra></extra>"
                                if index_mode
                                else "%{x|%Y-%m-%d %H:%M:%S}<br>%{y:.4f} "
                                + unit
                                + "（旧参数算出，未重算）<extra></extra>"
                            ),
                        )
                    )
                if limit is not None:
                    warn, alert, _direction, source = limit
                    for value, color, text in (
                        (warn, "#ffb74d", f"warn {warn:g}"),
                        (alert, "#ef5350", f"alert {alert:g}"),
                    ):
                        fig.add_hline(
                            y=value,
                            line={"color": color, "dash": "dash", "width": 1},
                            annotation_text=f"{text}（{source}）",
                            annotation_position="top left",
                            annotation_font={"color": color, "size": 11},
                        )
                detail.extend(
                    trend_rows.detail_rows(
                        rows,
                        channel=channel_name,
                        code=code,
                        limit=limit,
                        object_of_channel_node=OBJECT_OF_CHANNEL,
                    )
                )
            fig.update_layout(
                height=315,
                margin={"l": 10, "r": 12, "t": 30, "b": 10},
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis={
                    "title": {"text": axis_title if plotted else "北京时间", "standoff": 8},
                    "gridcolor": "#2a3441",
                    **({"tickformat": "%H:%M:%S"} if x_mode == trend_view.X_MODE_TIME else {}),
                },
                yaxis={"title": {"text": unit, "standoff": 8}, "gridcolor": "#2a3441"},
            )
            st.plotly_chart(
                fig,
                width="stretch",
                key=f"trend_chart_{target['node_id']}_{picked_node.id}",
            )

        # ---------------------------------------------------------------- 重算（S4A）
        st.divider()
        _recompute_block(
            [(t["channel_node_id"], t["code"]) for t in picked_targets],
            key=f"trend_recompute_{picked_node.id}",
            note=("参数 / 转速改过以后还没重算，图上画成灰色空心点；重算按当前生效参数覆盖为新值"),
            # 导出与重算同一行（马哥 2026-11-04）；明细为空时只显示重算
            export=(
                (
                    f"趋势_{scope_name}.csv",
                    pd.DataFrame(detail).to_csv(index=False).encode("utf-8-sig"),
                )
                if detail
                else None
            ),
        )

    with tab_rank:
        states = [item.state for item in health.values()]
        with store_ctx.open_session() as session:
            stale_total = metric_store.count_stale(session, TASK.id)
        kpi_row(
            [
                (
                    "严重",
                    str(states.count("alert")),
                    "最近 5 点越过 alert",
                    "状态为 alert 的节点数：每个指标取最近 5 个点里最严重的一个"
                    "（跳过待重算的旧参数点），上层节点再取子树内最严重的一个",
                ),
                (
                    "警告",
                    str(states.count("warn")),
                    "最近 5 点越过 warn",
                    "状态为 warn 的节点数：同样是每个指标最近 5 个点里最严重的一个状态",
                ),
                (
                    "正常 / 数据异常 / 未设阈值",
                    f"{states.count('ok')} / {states.count('invalid')} / {states.count('unset')}",
                    None,
                    "数据异常 = 有阈值但算出来的值不是有限数（NaN / ±Inf，比如除零、信号全零），"
                    "这种点没法判级，也不会显示成绿色正常；未设阈值 = 该指标还没配 warn / alert",
                ),
                (
                    "待重算",
                    str(stale_total),
                    "由旧参数算出，可重算",
                    "「由旧参数算出」= 这些点算的时候用的指标参数 / 额定转速与现在不一致（stale），"
                    "不参与判级；重算会回读原始数据、按当前参数逐窗重算并覆盖旧点",
                ),
            ]
        )
        if stale_total:  # 马哥 2026-11-03：0 条时不占版面
            st.caption(
                f"⏳ 待重算 {stale_total} 条旧参数值：图上画成灰色空心点，重算后按当前参数出数"
            )

        # 按节点重算（选中节点子树里的全部通道 × 指标）
        picked_row = _BY_ID.get(picked_id)
        subtree_channels = [
            node.id
            for node in all_nodes
            if node.node_type == "channel"
            and (
                picked_row is not None
                and (node.path == picked_row.path or node.path.startswith(picked_row.path + "/"))
            )
        ]
        _recompute_block(
            [(channel_id, None) for channel_id in subtree_channels],
            key=f"rank_recompute_{picked_id}",
            note="按选中节点子树里的通道逐个重算，旧值覆盖、作废的旧点位软删",
        )

        over_rows: list[dict] = []
        with store_ctx.open_session() as session:
            # 每条序列只取本指标节点自己通道节点的行（P2 / S1）：同一通道挂两个对象时各算各的
            for item in metric_series(session, TASK.id, all_nodes):
                limit = limits.resolve_limit(session, TASK.id, item.metric_node.id, item.code)
                if limit is None:
                    continue
                for row in item.values:
                    level = point_level(row.value, limit)
                    if level not in {"warn", "alert"}:
                        continue
                    over_rows.append(
                        {
                            "监测对象": OBJECT_OF_CHANNEL.get(item.channel_node_id, ""),
                            "采集时间": row.computed_at,
                            "通道": item.channel_name,
                            "指标": item.code,
                            "值": round(row.value, 4),
                            "单位": row.unit,
                            "判定": tree_ui.STATE_TEXT[level],
                            "阈值": f"warn {limit[0]:g} / alert {limit[1]:g}",
                            "阈值来源": limit[3],
                        }
                    )

        section(
            f"超限记录（{len(over_rows)} 条）",
            "逐点列出；时间戳 = 起始采集时间 + 窗中心偏移",
        )
        if over_rows:
            st.dataframe(pd.DataFrame(over_rows), hide_index=True, width="stretch")

        with st.expander(f"节点评级（{len(health)} 个节点，含子树累计）", expanded=False):
            st.caption("超限条数按子树累计；状态取子树内最严重的一个")
            st.markdown(rank_table_html(snapshot, health), unsafe_allow_html=True)
