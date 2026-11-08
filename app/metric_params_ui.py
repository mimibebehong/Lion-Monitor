"""健康树「监测指标」页签：按指标 schema 动态渲染参数表单（方案文档 4.1 / 4.2）。

交互口径：

- ``group=basic`` 常改，直接展开；``group=advanced``（带宽/算法）默认折叠，
  标题写「使用默认值」，改动后变「已修改 N 项」（带橙色角标）
- 每项都有「恢复默认」（清掉该项覆盖，跟随指标定义默认值）
- 保存：只把覆盖项写进 ``metric_assign.params_json``；勾选「固化全部参数」则写全量快照
- 校验（不通过禁止保存，给原因）：band_high ≤ fs/2；window_s ≥ 10/band_low；step_s ≤ window_s
- 改带宽类参数先提示「将影响 N 个数据集，需重算」，确认后才落库
- 转速门控：拿不到转速时，含 ``scaling=speed`` 频率项的指标在下拉里标 🔒 并禁止装配
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store_ctx  # noqa: E402
import streamlit as st  # noqa: E402
from ui import flash, section  # noqa: E402

from lion_monitor import limits_rules  # noqa: E402
from lion_monitor.params import (  # noqa: E402
    default_params,
    diff_params,
    merge_params,
    needs_speed,
    prepare_params,
)
from lion_monitor.store import (  # noqa: E402
    component_freq,
    datasets,
    limits,
    metric_assign,
    metric_def,
    tree,
)
from lion_monitor.store.limits import LimitError  # noqa: E402
from lion_monitor.store.metric_assign import MetricAssignError  # noqa: E402
from lion_monitor.store.metric_def import MetricDefRow  # noqa: E402
from lion_monitor.store.tree import TreeError  # noqa: E402

RPM_ATTR = tree.RPM_ATTR  # 转速存在节点属性里（与计算层同一口径）
LOCK_ICON = "🔒"
MODIFIED_ICON = "🔶"
#: 转速来源的中文名（与 metric_value.rpm_source 取值一一对应）
RPM_SOURCE_TEXT = {"node": "本节点手填", "measured": "批次实测", "rated": "对象额定"}


# ---------------------------------------------------------------- 纯函数（可单测）


def recompute_needed(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    """保存后生效参数是否变化（变了就得按新参数重算历史指标值）。

    以前只认带宽类（``BANDWIDTH_KEYS``），计算时长/步长/窗函数/去直流改了会静默落库：
    历史值只被标 stale，界面不给任何提示。马哥要求统一——凡是生效参数有变化，
    都先弹「将影响 N 个数据集，需重算」的确认。
    """
    return bool(diff_params(current, stored))


def advanced_title(modified_keys: list[str]) -> str:
    """高级参数折叠区的标题：默认写「使用默认值」，改过写「已修改 N 项」。"""
    if modified_keys:
        return f"高级参数 · 已修改 {len(modified_keys)} 项 {MODIFIED_ICON}"
    return "高级参数（带宽/算法）· 使用默认值"


def metric_label(
    code: str,
    unit: str,
    *,
    modified: int = 0,
    locked: bool = False,
    bound: bool = True,
    rpm_missing: bool = False,
) -> str:
    """指标折叠标题：状态一眼可见（默认/已修改/固化/未绑定参数/缺转速）。"""
    tags: list[str] = []
    if not bound:
        tags.append("未绑定参数")
    elif locked:
        tags.append("已固化")
    elif modified:
        tags.append(f"已修改 {modified} 项 {MODIFIED_ICON}")
    else:
        tags.append("默认值")
    if rpm_missing:
        tags.append("⚠ 缺少转速")
    suffix = " · ".join(tags)
    return f"{code}（{unit}）· {suffix}" if unit else f"{code} · {suffix}"


def option_label(row: MetricDefRow, *, rpm_missing: bool) -> str:
    """添加下拉的选项文案：门控的指标标 🔒 并写明原因。"""
    base = f"{row.code}（{row.unit}）" if row.unit else row.code
    if rpm_missing and needs_speed(row.schema):
        return f"{base}　{LOCK_ICON} 需先设置转速"
    return base


def source_text(
    key: str, *, override: dict[str, Any], definition: dict[str, Any], locked: bool
) -> str:
    """参数来源标注：本节点已覆盖 / 来自定义 / 默认。"""
    if locked or key in override:
        return "本节点已覆盖"
    if key in definition:
        return "来自定义"
    return "默认"


# ---------------------------------------------------------------- 小工具


def _widget_key(assign_id: int, key: str) -> str:
    return f"mp_{assign_id}_{key}"


def _reset_widget(widget_key: str, value: Any) -> None:
    """「恢复默认」回调：必须在回调里改 widget 状态（运行中改会被 Streamlit 拒绝）。"""
    st.session_state[widget_key] = value


def _schema_for(row: MetricDefRow) -> list[dict[str, Any]]:
    return row.schema or []


def _base_defaults(row: MetricDefRow) -> dict[str, Any]:
    """非节点默认：硬默认 + 定义默认。"""
    return merge_params(default_params(row.code), row.defaults)


# ---------------------------------------------------------------- 参数表单


def _render_item(
    item: dict[str, Any],
    *,
    assign_id: int,
    default_value: Any,
    override: dict[str, Any],
    definition: dict[str, Any],
    locked: bool,
) -> None:
    """单个参数控件 + 来源标注 + 恢复默认。"""
    key = item["key"]
    widget_key = _widget_key(assign_id, key)
    label = f"{item['label']}（{item['unit']}）" if item.get("unit") else item["label"]
    kwargs: dict[str, Any] = {"key": widget_key, "help": item.get("help")}

    col_widget, col_meta = st.columns([0.66, 0.34], vertical_alignment="center")
    with col_widget:
        if item["type"] == "bool":
            st.checkbox(label, value=bool(default_value), **kwargs)
        elif item["type"] == "enum":
            options = list(item.get("options") or [])
            value = default_value if default_value in options else options[0]
            st.selectbox(label, options, index=options.index(value), **kwargs)
        else:
            step = 0.5 if item.get("unit") == "s" else 1.0
            lower = item.get("min")
            upper = item.get("max")
            shown = float(default_value)
            # 历史/定义默认里的越界值不能把 number_input 顶崩：先夹到控件区间
            if lower is not None:
                shown = max(float(lower), shown)
            if upper is not None:
                shown = min(float(upper), shown)
            st.number_input(
                label,
                value=shown,
                step=float(step),
                min_value=float(lower) if lower is not None else None,
                max_value=float(upper) if upper is not None else None,
                **kwargs,
            )
    with col_meta:
        c_text, c_reset = st.columns([0.55, 0.45], vertical_alignment="center")
        c_text.caption(source_text(key, override=override, definition=definition, locked=locked))
        c_reset.button(
            "恢复默认",
            key=f"mp_reset_{assign_id}_{key}",
            on_click=_reset_widget,
            args=(widget_key, item.get("default")),
            use_container_width=True,
        )


def _collect(
    schema: list[dict[str, Any]], assign_id: int, effective: dict[str, Any]
) -> dict[str, Any]:
    """当前表单值：widget 状态优先，其次生效参数（首屏就是库里已保存的值）。"""
    values: dict[str, Any] = {}
    for item in schema:
        key = item["key"]
        fallback = effective.get(key, item.get("default"))
        values[key] = st.session_state.get(_widget_key(assign_id, key), fallback)
    return values


def _persist(session, row, full: dict[str, Any], locked: bool, base: dict[str, Any]) -> None:
    """落库：固化写全量，未固化只写覆盖项。"""
    if locked != row.params_locked:
        metric_assign.set_locked(session, row.id, locked)
    payload = full if locked else diff_params(full, base)
    metric_assign.update_params(session, row.id, payload)


def _render_bound_metric(
    *,
    task_id: int,
    metric_node,
    row,
    def_row: MetricDefRow | None,
    effective: dict[str, Any],
    rpm: float | None,
    sample_rate: float | None,
    dataset_count: int,
) -> None:
    """一个已装配指标的参数块（只拿数据，不开会话，写操作用短会话）。"""
    schema = _schema_for(def_row) if def_row is not None else []
    base = _base_defaults(def_row) if def_row is not None else {}

    if row is None:
        title = metric_label(metric_node.name, "", bound=False)
        with st.expander(title, expanded=False):
            st.caption("该指标节点是早期/模板导入建的，还没有参数绑定；绑定后可按 schema 改参数")
            if st.button("绑定参数", key=f"mp_bind_{metric_node.id}"):
                with store_ctx.open_session() as session:
                    metric_assign.assign_metric(
                        session, task_id, metric_node.parent_id, metric_node.name
                    )
                flash(f"已绑定参数：{metric_node.name}")
                st.rerun()
        return

    locked = row.params_locked
    override = row.params if locked else row.params
    current = _collect(schema, row.id, effective)
    modified = diff_params(current, base) if schema else {}
    _, _, raise_note = prepare_params(current, sample_rate)
    rpm_missing = def_row is not None and needs_speed(schema) and rpm is None
    title = metric_label(
        row.code,
        row.unit,
        modified=len(modified),
        locked=locked,
        rpm_missing=rpm_missing,
    )

    with st.expander(title, expanded=False):
        if rpm_missing:
            st.error("缺少转速，无法计算：先在通道/监测对象填转速，或等批次实测转速到达")
        if raise_note:
            st.info(f"{raise_note}（保存后按该值生效）")
        if not schema:
            st.caption("该指标没有声明参数 schema，按系统默认值计算")
            return

        basic = [item for item in schema if item.get("group") == "basic"]
        advanced = [item for item in schema if item.get("group") == "advanced"]

        if basic:
            st.caption("基本参数")
            for item in basic:
                _render_item(
                    item,
                    assign_id=row.id,
                    default_value=effective.get(item["key"], item.get("default")),
                    override=override,
                    definition=(def_row.defaults if def_row else {}),
                    locked=locked,
                )

        if advanced:
            adv_modified = [key for key in modified if key in {item["key"] for item in advanced}]
            with st.expander(advanced_title(adv_modified), expanded=bool(adv_modified)):
                for item in advanced:
                    _render_item(
                        item,
                        assign_id=row.id,
                        default_value=effective.get(item["key"], item.get("default")),
                        override=override,
                        definition=(def_row.defaults if def_row else {}),
                        locked=locked,
                    )

        lock_flag = st.checkbox(
            "固化全部参数（此后不再跟随指标定义）",
            value=locked,
            key=f"mp_lock_{row.id}",
            help="固化后把当前全部生效值写进本节点，指标定义改默认值也不影响它",
        )

        if sample_rate is None:
            st.caption("还没有导入数据：带通上限需等采样率已知后再校验")

        pending_key = f"mp_pending_{row.id}"
        pending = st.session_state.get(pending_key)

        if pending is None:
            if st.button("保存参数", key=f"mp_save_{row.id}"):
                prepared, problems, note = prepare_params(current, sample_rate)
                if problems:
                    for text in problems:
                        st.error(f"参数校验未通过：{text}")
                elif recompute_needed(effective, prepared):
                    st.session_state[pending_key] = {
                        "full": prepared,
                        "locked": lock_flag,
                        "note": note,
                    }
                    st.rerun()
                else:
                    with store_ctx.open_session() as session:
                        _persist(session, row, prepared, lock_flag, base)
                    flash(f"已保存 {row.code} 的参数" + (f"；{note}" if note else ""))
                    st.rerun()
        else:
            st.warning(
                f"将影响 {dataset_count} 个数据集，需重算：参数改动后历史指标值需按新参数重算"
            )
            c_ok, c_cancel = st.columns(2)
            if c_ok.button("确认保存（需重算）", key=f"mp_confirm_{row.id}"):
                st.session_state.pop(pending_key, None)
                with store_ctx.open_session() as session:
                    _persist(session, row, pending["full"], pending["locked"], base)
                note = pending.get("note")
                flash(
                    f"已保存 {row.code} 的参数，{dataset_count} 个数据集待重算"
                    + (f"；{note}" if note else "")
                )
                st.rerun()
            if c_cancel.button("取消", key=f"mp_cancel_{row.id}"):
                st.session_state.pop(pending_key, None)
                st.rerun()


def _render_add_metric(
    *, task_id: int, channel_node, defs: list[MetricDefRow], bound: set[str], rpm
) -> None:
    """添加指标：门控的指标标 🔒，装配时同时建指标节点与参数绑定。"""
    candidates = [row for row in defs if row.code not in bound and row.enabled]
    st.caption("添加指标：表单按指标定义 schema 自动渲染；🔒 表示需先设置转速")
    if not candidates:
        st.caption("台账里的指标都已装配；要加新指标先去「指标配置」建")
        return

    labels = [option_label(row, rpm_missing=rpm is None) for row in candidates]
    chosen_label = st.selectbox(
        "选择指标（来自台账）", labels, key=f"mp_add_pick_{channel_node.id}"
    )
    chosen = candidates[labels.index(chosen_label)]
    if rpm is None and needs_speed(chosen.schema):
        st.caption(
            f"{LOCK_ICON} {chosen.code} 需要转速：先在「监测对象」节点设额定转速，或等批次实测转速"
        )

    if st.button("添加指标", key=f"mp_add_btn_{channel_node.id}"):
        if rpm is None and needs_speed(chosen.schema):
            st.error(f"需先设置转速：{chosen.code} 含按转速缩放的特征频率项，未设置转速不能装配")
            return
        try:
            with store_ctx.open_session() as session:
                tree.create_node(
                    session,
                    task_id,
                    "metric",
                    chosen.code,
                    parent_id=channel_node.id,
                    attrs={"code": chosen.code, "单位": chosen.unit},
                    ref_type="metric_def",
                    ref_id=chosen.id,
                )
                metric_assign.assign_metric(session, task_id, channel_node.id, chosen.code)
        except (TreeError, MetricAssignError) as exc:
            st.error(str(exc))
        else:
            flash(f"已添加指标：{chosen.code}")
            st.rerun()


# ---------------------------------------------------------------- 页签入口


def _param_summary(effective: dict[str, Any], row, limit=None) -> str:
    """通道汇总里的一行参数摘要（只读）。"""
    if row is None:
        return "⚠️ 未绑定参数（点「配置参数」补上）"
    parts: list[str] = []
    if isinstance(effective.get("window_s"), (int, float)):
        parts.append(f"窗长 {float(effective['window_s']):g} s")
    if isinstance(effective.get("step_s"), (int, float)):
        parts.append(f"步长 {float(effective['step_s']):g} s")
    low, high = effective.get("band_low_hz"), effective.get("band_high_hz")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        parts.append(f"{float(low):g}–{float(high):g} Hz")
    if row.params_locked:
        parts.append("🔒 固化")
    if limit:
        warn, alert, _direction, _source = limit
        parts.append(f"阈值 {warn:g}/{alert:g}")
    else:
        parts.append("无阈值")
    return " · ".join(parts) if parts else "跟随指标默认"


def render(task_id: int, channel_node) -> None:
    """通道节点的「监测指标」页签：只读汇总 + 跳转到指标节点编辑参数。

    马哥 2026-11-02：参数配置下放到指标节点，通道这一级只汇总与装配。
    """
    channel_code = channel_node.name
    with store_ctx.open_session() as session:
        nodes = tree.list_nodes(session, task_id)
        defs = metric_def.list_metrics(session, task_id)
        def_by_code = {row.code: row for row in defs}
        assign_by_id = {
            row.node_id: row for row in metric_assign.list_assignments(session, task_id)
        }
        effective_by_id = {
            node_id: metric_assign.effective_params(session, row.id)
            for node_id, row in assign_by_id.items()
        }
        effective_rpm, effective_source = component_freq.resolve_node_rpm(session, channel_node.id)
        sample_rate = datasets.channel_sample_rate(session, task_id, channel_code)
        metric_nodes = [
            node
            for node in nodes
            if node.node_type == "metric" and node.parent_id == channel_node.id
        ]
        codes_by_node = {
            node.id: ((node.attrs.get("code") or node.name) or node.name) for node in metric_nodes
        }
        limit_by_node = limits.resolve_limits(session, task_id, codes_by_node, nodes)

    metric_nodes = [
        node for node in nodes if node.node_type == "metric" and node.parent_id == channel_node.id
    ]
    section(
        "监测指标",
        f"{len(metric_nodes)} 个指标　采样率 "
        + (f"{sample_rate:g} Hz" if sample_rate else "未知（未导入数据）"),
    )

    rpm_text = f"{effective_rpm:g} rpm" if effective_rpm is not None else "未设置"
    source_text = RPM_SOURCE_TEXT.get(effective_source or "", "未设置")
    st.caption(f"生效转速：{rpm_text}（来源：{source_text}）· 额定转速在「监测对象」节点设置")

    if not metric_nodes:
        st.caption("这个通道还没有装配指标：在下面选一个添加")

    for node in metric_nodes:
        code = (node.attrs.get("code") or node.name) or node.name
        row = assign_by_id.get(node.id)
        unit = def_by_code[code].unit if code in def_by_code else (row.unit if row else "—")
        cols = st.columns([0.14, 0.08, 0.44, 0.17, 0.17], vertical_alignment="center")
        cols[0].markdown(f"**{code}**")
        cols[1].caption(unit or "—")
        cols[2].caption(
            _param_summary(effective_by_id.get(node.id, {}), row, limit_by_node.get(node.id))
        )
        if cols[3].button("配置参数 →", key=f"mp_go_{node.id}", width="stretch"):
            st.session_state["ht_selected"] = node.id
            st.rerun()
        if cols[4].button("移除", key=f"mp_rm_{node.id}", width="stretch"):
            st.session_state[f"mp_rm_pending_{node.id}"] = True
            st.rerun()

    pending = [node for node in metric_nodes if st.session_state.get(f"mp_rm_pending_{node.id}")]
    if pending:
        target = pending[0]
        target_code = (target.attrs.get("code") or target.name) or target.name
        st.warning(
            f"移除指标节点「{target_code}」？它的参数卡与阈值会一起进回收站（可恢复），"
            "通道本身和传感器不受影响。"
        )
        col_ok, col_cancel = st.columns(2)
        if col_ok.button("确认移除", key=f"mp_rm_ok_{target.id}"):
            try:
                with store_ctx.open_session() as session:
                    tree.soft_delete_node(session, target.id, operator="小智")
            except (TreeError, MetricAssignError) as exc:
                st.error(str(exc))
            else:
                st.session_state.pop(f"mp_rm_pending_{target.id}", None)
                flash(f"已移除指标节点：{target_code}（回收站可恢复）")
                st.rerun()
        if col_cancel.button("取消", key=f"mp_rm_no_{target.id}"):
            st.session_state.pop(f"mp_rm_pending_{target.id}", None)
            st.rerun()

    st.divider()
    bound = {(node.attrs.get("code") or node.name) or node.name for node in metric_nodes}
    _render_add_metric(
        task_id=task_id,
        channel_node=channel_node,
        defs=defs,
        bound=bound,
        rpm=effective_rpm,
    )


def render_metric_node(task_id: int, metric_node) -> None:
    """指标节点的「指标参数」页签：这个指标的参数在这里编辑（唯一编辑入口）。"""
    code = (metric_node.attrs.get("code") or metric_node.name) or metric_node.name
    with store_ctx.open_session() as session:
        nodes = tree.list_nodes(session, task_id)
        defs = metric_def.list_metrics(session, task_id)
        def_by_code = {row.code: row for row in defs}
        parent = next((row for row in nodes if row.id == metric_node.parent_id), None)
        channel_code = parent.name if parent is not None else code
        row = next(
            (
                item
                for item in metric_assign.list_assignments(session, task_id)
                if item.node_id == metric_node.id
            ),
            None,
        )
        effective = metric_assign.effective_params(session, row.id) if row is not None else {}
        effective_rpm, effective_source = component_freq.resolve_node_rpm(session, metric_node.id)
        sample_rate = datasets.channel_sample_rate(session, task_id, channel_code)
        dataset_count = datasets.count_datasets_for_channel(session, task_id, channel_code)

    section("指标参数", f"{code} · 通道 {channel_code}")
    rpm_text = f"{effective_rpm:g} rpm" if effective_rpm is not None else "未设置"
    source_text = RPM_SOURCE_TEXT.get(effective_source or "", "未设置")
    st.caption(f"生效转速：{rpm_text}（来源：{source_text}）· 额定转速在「监测对象」节点设置")

    _render_bound_metric(
        task_id=task_id,
        metric_node=metric_node,
        row=row,
        def_row=def_by_code.get(code),
        effective=effective,
        rpm=effective_rpm,
        sample_rate=sample_rate,
        dataset_count=dataset_count,
    )
    unit = getattr(def_by_code.get(code), "unit", "") or ""
    _render_limit_inline(task_id, metric_node, code, unit)


def _render_limit_inline(task_id: int, metric_node, code: str, unit: str = "") -> None:
    """指标节点自己的阈值（warn / alert）；没有就按上级继承，删除即回到继承。"""
    with store_ctx.open_session() as session:
        own = next(
            (
                row
                for row in limits.list_limits(session, task_id)
                if row.node_id == metric_node.id and row.metric_code == code
            ),
            None,
        )
        inherited = limits.resolve_limit(session, task_id, metric_node.id, code)

    section("阈值", "属于这个指标；通道/对象级的批量维护仍去「阈值设置」页")
    if own is not None:
        st.caption(
            f"当前是自定义阈值：warn {own.warn:g} / alert {own.alert:g}"
            f"（{limits_rules.DIRECTION_TEXT[limits_rules.normalize_direction(own.direction)]}）"
        )
    elif inherited is not None:
        warn, alert, direction, source = inherited
        label = limits_rules.DIRECTION_TEXT[limits_rules.normalize_direction(direction)]
        st.caption(f"当前生效：warn {warn:g} / alert {alert:g}（{label}，继承自{source}）")
    else:
        st.caption("还没有阈值：填 warn / alert 后保存")

    # 没有自定义也没有继承时留空（None），不要预填 0.000 —— 0 本身就不合法，会误导
    default_warn = own.warn if own is not None else (inherited[0] if inherited else None)
    default_alert = own.alert if own is not None else (inherited[1] if inherited else None)
    current = own.direction if own is not None else (inherited[2] if inherited else ">")
    choices = list(limits_rules.DIRECTION_TEXT.values())
    kinds = list(limits_rules.DIRECTION_TEXT)
    index = kinds.index(limits_rules.normalize_direction(current))
    suffix = f"（{unit}）" if unit else ""

    with st.form(f"mp_limit_{metric_node.id}"):
        col_warn, col_alert, col_dir = st.columns([0.28, 0.28, 0.44], vertical_alignment="bottom")
        warn_in = col_warn.number_input(
            f"warn{suffix}",
            value=None if default_warn is None else float(default_warn),
            min_value=0.0,
            step=0.1,
            format="%.3f",
            placeholder="未设置",
        )
        alert_in = col_alert.number_input(
            f"alert{suffix}",
            value=None if default_alert is None else float(default_alert),
            min_value=0.0,
            step=0.1,
            format="%.3f",
            placeholder="未设置",
        )
        choice = col_dir.selectbox("方向", choices, index=index)
        col_save, col_drop = st.columns(2)
        save = col_save.form_submit_button("保存阈值", width="stretch")
        drop = col_drop.form_submit_button("删除阈值（回到继承）", width="stretch")

    if save and (warn_in is None or alert_in is None):
        st.error("先填 warn 和 alert 再保存；不想要自定义阈值就点「删除阈值（回到继承）」")
    elif save:
        direction = next(key for key, text in limits_rules.DIRECTION_TEXT.items() if text == choice)
        try:
            with store_ctx.open_session() as session:
                limits.set_limit(
                    session,
                    task_id,
                    metric_node.id,
                    code,
                    float(warn_in),
                    float(alert_in),
                    direction,
                )
        except LimitError as exc:
            st.error(str(exc))
        else:
            flash(f"已保存 {code} 的阈值")
            st.rerun()
    if drop:
        with store_ctx.open_session() as session:
            for row in limits.list_limits(session, task_id):
                if row.node_id == metric_node.id and row.metric_code == code:
                    limits.delete_limit(session, row.id)
        flash(f"已删除 {code} 的自定义阈值，回到上级继承")
        st.rerun()
