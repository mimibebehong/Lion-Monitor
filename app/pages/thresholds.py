"""阈值设置：表格内直接输入 warn / alert，通道与指标只读；切换筛选前拦截未保存改动。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
import ui
from ui import (  # noqa: E402
    danger_button_css,
    flash,
    hint,
    page_header,
    section,
    selectable_table,
    show_flash,
)

from lion_monitor import limits_rules  # noqa: E402
from lion_monitor.store import audit, limits, metric_def, object_registry, tree  # noqa: E402
from lion_monitor.store.limits import LimitError  # noqa: E402

EDIT_KEY = "limit_editor"
FILTER_KEYS = ("lim_obj", "lim_chan", "lim_metric")

#: 方向下拉：显示人话，保存时用 limits_rules 映射回库里的 > / <
DIRECTION_CHOICES = tuple(limits_rules.DIRECTION_TEXT.values())


def _direction_of_choice(choice: object, fallback: str = "upper") -> str:
    """下拉显示值 → 规范方向（认不出就用 fallback，不猜）。"""
    text = str(choice or "").strip()
    for kind, label in limits_rules.DIRECTION_TEXT.items():
        if text == label:
            return kind
    return limits_rules.normalize_direction(fallback)


task = store_ctx.current_task()
if task is None:
    page_header("阈值设置", "还没有测试任务，先去「任务列表」新建一个", demo=False)
    st.stop()

with store_ctx.open_session() as session:
    nodes = tree.list_nodes(session, task.id)
    stored = limits.list_limits(session, task.id)
    # 监测对象读台账（monitor_object），与「监测对象」页同一数据源（G4）
    objs = object_registry.list_objects(session, task.id)
    metric_reg = metric_def.list_metrics(session, task.id)

page_header(
    "阈值设置",
    "表格里直接改 warn / alert；两列都清空 = 删除该行阈值、回到继承",
    badge=f"{len(stored)} 条",
    demo=False,
)
show_flash()

by_path = {n.id: n for n in nodes}
channels = [n for n in nodes if n.node_type == "channel"]
# 台账对象 ↔ 树里对应的组件节点：节点 ref_id 指向台账主数据；老树没 ref 的按名称兜底
component_nodes: dict[int, object_registry.ObjectRow] = {}  # 组件节点 id → 台账对象
component_node_of: dict[int, int] = {}  # 台账对象 id → 组件节点 id
for obj in objs:
    hit = next(
        (
            node
            for node in nodes
            if node.node_type == "component"
            and (node.ref_id == obj.id or (node.ref_id is None and node.name == obj.name))
        ),
        None,
    )
    if hit is not None:
        component_nodes[hit.id] = obj
        component_node_of[obj.id] = hit.id

metric_codes = [m.code for m in metric_reg] or [
    n.attrs.get("code") for n in nodes if n.node_type == "metric" and n.attrs.get("code")
]
units = {m.code: m.unit for m in metric_reg}
# 指标节点：阈值统一挂在「通道 × 指标」对应的指标节点上（最细颗粒度）
metric_node_of: dict[tuple[int, str], int] = {}
for node in nodes:
    if node.node_type != "metric" or node.parent_id is None:
        continue
    code = (node.attrs or {}).get("code") or node.name
    metric_node_of[(node.parent_id, code)] = node.id

stored_index = {(row.node_id, row.metric_code): row for row in stored}


def _drop_and_switch() -> None:
    """放弃表格改动并把用户原本想切换的筛选值装上（必须在回调里改控件状态）。"""
    st.session_state.pop(EDIT_KEY, None)
    pending = st.session_state.pop("lim_pending", None)
    if pending:
        for key, value in zip(FILTER_KEYS, pending, strict=True):
            st.session_state[key] = value
        st.session_state["lim_prev"] = tuple(pending)
    st.session_state.pop("lim_block_count", None)


def _edited_count() -> int:
    state = st.session_state.get(EDIT_KEY)
    if isinstance(state, dict):
        return len(state.get("edited_rows") or {})
    return 0


def _on_filter_change() -> None:
    """筛选变了但表格有未保存改动：撤回本次切换，记下用户原本想去哪。"""
    prev = st.session_state.get("lim_prev")
    cur = tuple(st.session_state.get(key) for key in FILTER_KEYS)
    if prev and cur != prev and _edited_count() > 0:
        st.session_state["lim_pending"] = cur
        st.session_state["lim_block_count"] = _edited_count()
        for key, value in zip(FILTER_KEYS, prev, strict=True):
            st.session_state[key] = value
    else:
        st.session_state["lim_prev"] = cur


for key in FILTER_KEYS:
    st.session_state.setdefault(key, "全部")

obj_names = ["全部"] + [o.name for o in objs]
chan_names = ["全部"] + [ch.name for ch in channels]
metric_names = ["全部"] + list(metric_codes)

c1, c2, c3 = st.columns(3)
c1.selectbox("监测对象", obj_names, key="lim_obj", on_change=_on_filter_change)
c2.selectbox("通道", chan_names, key="lim_chan", on_change=_on_filter_change)
c3.selectbox("指标", metric_names, key="lim_metric", on_change=_on_filter_change)

if "lim_prev" not in st.session_state:
    st.session_state["lim_prev"] = tuple(st.session_state.get(k) for k in FILTER_KEYS)

if st.session_state.get("lim_pending"):
    blocked = st.session_state.get("lim_block_count", 0)
    ui.warning(
        f"检测到 **{blocked} 行**未保存的修改，已阻止切换筛选。"
        "请先点「保存修改」，或点下面的「放弃改动并切换」。"
    )
    st.button("放弃改动并切换", key="lim_drop", on_click=_drop_and_switch)

obj_pick = st.session_state["lim_obj"]
chan_pick = st.session_state["lim_chan"]
metric_pick = st.session_state["lim_metric"]

# 阈值列表也只看台账里的对象：树里没有台账的遗留组件不出现在阈值页（与「监测对象」页逐项一致）
chan_pool = [ch for ch in channels if ch.parent_id in component_nodes]
if obj_pick != "全部":
    target = next((o for o in objs if o.name == obj_pick), None)
    anchor_id = component_node_of.get(target.id) if target is not None else None
    anchor = by_path.get(anchor_id) if anchor_id is not None else None
    prefix = f"{anchor.path}/" if anchor is not None else None
    # 台账里有、健康树还没装配的对象 → 名下没有任何通道（不能拿别的对象的数据冒充）
    chan_pool = [ch for ch in chan_pool if prefix and ch.path.startswith(prefix)]

targets = [
    (ch, code)
    for ch in chan_pool
    if chan_pick == "全部" or ch.name == chan_pick
    for code in metric_codes
    if (metric_pick == "全部" or code == metric_pick) and (ch.id, code) in metric_node_of
]

resolved: list[dict] = []
#: 老库里方向与数值关系不符的行：只提示人工确认，读的时候不改数
legacy_notes: list[str] = []
with store_ctx.open_session() as session:
    for ch, code in targets:
        hit = limits.resolve_limit(session, task.id, metric_node_of[(ch.id, code)], code)
        parent = by_path.get(ch.parent_id)
        owner = component_nodes.get(ch.parent_id or 0)
        if hit is not None:
            problem = limits_rules.problem_of(hit[2], hit[0], hit[1])
            if problem is not None:
                legacy_notes.append(f"{ch.name} · {code}：{problem}")
        resolved.append(
            {
                "监测对象": owner.name if owner else (parent.name if parent else "—"),
                "部位": (owner.part or "—") if owner else "—",
                "通道": ch.name,
                "指标": code,
                "单位": units.get(code, ""),
                "warn": float(hit[0]) if hit else float("nan"),
                "alert": float(hit[1]) if hit else float("nan"),
                "方向": limits_rules.text_of(hit[2]) if hit else limits_rules.text_of("upper"),
                "来源": hit[3] if hit else "未设阈值",
            }
        )

if legacy_notes:
    detail = "\n".join(f"- {note}" for note in legacy_notes)
    ui.warning(
        f"以下 **{len(legacy_notes)} 行**阈值的方向与数值关系不符（老数据，数值未改动），"
        f"请人工确认后手工改正：\n\n{detail}",
        icon="⚠️",
    )

section(
    "阈值表",
    f"{len(resolved)} 行（通道 × 指标，逐指标节点配置）；warn / alert / 方向 可编辑，其余只读",
)
if not resolved:
    st.caption("当前筛选没有可显示的通道 × 指标组合；先去「健康树配置」装配通道与指标")
    st.stop()

frame = pd.DataFrame(resolved)
# 0 = 该档不判（单边设置），回显成空而不是 "0"，否则看起来像「阈值 0」
frame["warn"] = ["" if pd.isna(v) or not v else f"{v:g}" for v in frame["warn"]]
frame["alert"] = ["" if pd.isna(v) or not v else f"{v:g}" for v in frame["alert"]]

edited = st.data_editor(
    frame,
    key=EDIT_KEY,
    hide_index=True,
    width="stretch",
    disabled=["监测对象", "部位", "通道", "指标", "单位", "来源"],
    column_config={
        "warn": st.column_config.TextColumn(
            "warn", help="留空=该档不判（可只设 alert）；填较轻的门槛"
        ),
        "alert": st.column_config.TextColumn(
            "alert", help="留空=该档不判（可只设 warn）；填更严重的门槛"
        ),
        "方向": st.column_config.SelectboxColumn(
            "方向",
            options=list(DIRECTION_CHOICES),
            help="上限（越大越差）：warn < alert；下限（越小越差）：warn > alert",
        ),
    },
)

# 逐行方向说明：只要有行选了「下限」，就把该方向的填数口径写在表格下面
if any(_direction_of_choice(row["方向"]) == "lower" for _, row in edited.iterrows()):
    hint(
        "下限（越小越差）：warn 填较轻的门槛（数值较大），alert 填更严重的门槛（数值较小），"
        "即要求 warn > alert；单边只填一个时，留空的一档不判级",
        icon="↘",
    )

dirty = _edited_count()
left, right = st.columns([0.32, 0.68])
if left.button("保存修改", key="lim_save", disabled=dirty == 0):
    errors: list[str] = []
    writes: list[tuple[str, int, str, float, float, str]] = []

    for idx, (ch, code) in enumerate(targets):
        row = edited.iloc[idx]
        warn_raw = str(row["warn"] or "").strip()
        alert_raw = str(row["alert"] or "").strip()
        warn_empty, alert_empty = warn_raw == "", alert_raw == ""
        metric_node_id = metric_node_of[(ch.id, code)]
        existing = stored_index.get((metric_node_id, code))
        direction = _direction_of_choice(
            row["方向"], fallback=existing.direction if existing else ">"
        )

        if warn_empty and alert_empty:
            if existing is not None:
                writes.append(("delete", metric_node_id, code, 0.0, 0.0, direction))
            continue
        # 马哥 2026-11-04：可以只设 warn 或只设 alert，留空的一侧记 0 = 该档不判
        try:
            warn_f = float(warn_raw) if warn_raw else 0.0
            alert_f = float(alert_raw) if alert_raw else 0.0
        except ValueError:
            errors.append(f"{ch.name} · {code}：warn / alert 必须是数字")
            continue
        if warn_f < 0 or alert_f < 0:
            errors.append(f"{ch.name} · {code}：阈值不能为负")
            continue
        # 大小关系按方向校验：口径统一走 limits_rules（上限 warn<alert、下限 warn>alert）
        try:
            limits_rules.validate_limit(direction, warn_f, alert_f)
        except LimitError as exc:
            errors.append(f"{ch.name} · {code}：{exc}")
            continue
        changed = (
            existing is None
            or existing.warn != warn_f
            or existing.alert != alert_f
            or limits_rules.normalize_direction(existing.direction)
            != limits_rules.normalize_direction(direction)
        )
        if changed:
            writes.append(("set", metric_node_id, code, warn_f, alert_f, direction))

    if errors:
        ui.error("；".join(errors))
    elif not writes:
        st.info("表格里没有实际变化，未写入")
    else:
        try:
            with store_ctx.open_session() as session:
                for kind, node_id, code, warn_f, alert_f, direction in writes:
                    if kind == "delete":
                        hit = stored_index.get((node_id, code))
                        if hit is not None:
                            limits.delete_limit(session, hit.id)
                    else:
                        limits.set_limit(
                            session,
                            task.id,
                            node_id,
                            code,
                            warn=warn_f,
                            alert=alert_f,
                            direction=direction,
                        )
        except LimitError as exc:
            ui.error(str(exc))
        else:
            st.session_state.pop(EDIT_KEY, None)
            flash(f"已保存 {len(writes)} 行阈值")
            st.rerun()

hint(
    "阈值统一挂在「通道 × 指标」的指标节点上（来源显示 指标级）；"
    "表里没有的指标说明健康树没挂这个组合。两列都清空 = 删除该行阈值、回到继承。"
)

with st.expander(f"已存阈值（{len(stored)} 条）", expanded=False):
    if not stored:
        st.caption("还没有写入任何阈值；直接在表格里填 warn / alert 即可")
    else:
        df = pd.DataFrame(
            [
                {
                    "节点": row.node_name,
                    "类型": row.source,
                    "指标": row.metric_code,
                    "warn": row.warn,
                    "alert": row.alert,
                    "方向": limits_rules.text_of(row.direction),
                }
                for row in stored
            ]
        )
        asked = selectable_table(df, key="limit_table")
        if asked:
            row = stored[asked[-1]]
            # 删除二合一（与监测对象 / 传感器 / 任务页一致）：第一次变红「确认删除？」，
            # 第二次才真删；armed 按阈值行 id 存，切到另一行自然复位
            arm_key = f"del_limit_armed_{row.id}"
            armed = bool(st.session_state.get(arm_key))
            if armed:
                st.markdown(
                    danger_button_css(f"del_limit_btn_{row.id}"),
                    unsafe_allow_html=True,
                )
            if st.button(
                "确认删除？" if armed else "删除阈值",
                key=f"del_limit_btn_{row.id}",
                help="点两次删除：第一次进入待确认，第二次执行",
            ):
                if not armed:
                    st.session_state[arm_key] = True
                    st.rerun()
                with store_ctx.open_session() as session:
                    limits.delete_limit(session, row.id)
                st.session_state.pop(arm_key, None)
                flash("已删除阈值", icon="🗑️")
                st.rerun()

# ---------------------------------------------------------------- 变更历史（T3）
st.divider()
with st.expander(f"变更历史（最近 {audit.DEFAULT_LIMIT} 条）", expanded=False):
    entity_choice = st.selectbox(
        "实体类型",
        ("全部", *audit.ENTITY_TEXT.values()),
        key="chg_entity",
        help=(
            "留痕来自 change_log：阈值 / 指标参数 / 指标定义默认值 / 节点转速 / 指标重算的真实变化"
        ),
    )
    entity_type = next(
        (key for key, label in audit.ENTITY_TEXT.items() if label == entity_choice), None
    )
    with store_ctx.open_session() as session:
        changes = audit.list_changes(session, entity_type=entity_type, limit=audit.DEFAULT_LIMIT)
    if not changes:
        hint(
            "还没有变更留痕：改阈值、指标参数、指标定义默认值、节点转速或重算指标后，这里按时间倒序列出"
        )
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "时间": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "实体": row.entity_text,
                        "对象": row.note or "—",
                        "字段": row.field,
                        "旧值 → 新值": (
                            f"{audit.value_text(row.old_value)} → {audit.value_text(row.new_value)}"
                        ),
                        "操作人": row.actor or "—",
                    }
                    for row in changes
                ]
            ),
            key="change_history",
            hide_index=True,
            width="stretch",
        )
        st.caption("倒序（新的在上），最多 200 条；参数类改动合成一条，旧值/新值只列改动的键")
