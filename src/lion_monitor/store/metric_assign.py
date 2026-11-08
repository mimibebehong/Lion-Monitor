"""指标授予：给通道分配要计算的指标与参数（指标码以台账为准）。

参数口径（方案文档 2.2 / 3.1）：

- ``metric_assign.params_json`` 只存**覆盖项**（与生效默认不同的键）
- ``params_locked=0``：生效参数 = 硬默认 < 指标定义默认 < 本节点覆盖
- ``params_locked=1``：``params_json`` 是全量固化快照，定义改默认值不再影响本节点

生效参数从 ``effective_params`` 取，计算层拿它算指标、写参数指纹。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import METRIC_UNITS
from ..params import default_params, diff_params, merge_params
from . import audit, metric_def, tree
from . import metrics as metric_store
from .models import MetricAssign

__all__ = [
    "METRIC_CODES",
    "MetricAssignError",
    "MetricRow",
    "assign_metric",
    "bind_missing",
    "compose_params",
    "definition_defaults",
    "effective_params",
    "find_assignment",
    "list_assignments",
    "mark_stale_for_definition_change",
    "missing_bindings",
    "set_locked",
    "set_node_rpm",
    "unassign",
    "update_params",
]

#: 内置指标码与默认单位（别名，真定义在 ``lion_monitor.constants``）
METRIC_CODES: dict[str, str] = METRIC_UNITS


class MetricAssignError(Exception):
    """指标授予业务错误。"""


@dataclass(frozen=True)
class MetricRow:
    id: int
    task_id: int
    node_id: int
    channel_name: str
    code: str
    unit: str
    params: dict[str, Any] = field(default_factory=dict)
    params_locked: bool = False
    enabled: bool = True


def _json_of(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _to_row(model: MetricAssign, channel_name: str) -> MetricRow:
    return MetricRow(
        id=model.id,
        task_id=model.task_id,
        node_id=model.node_id,
        channel_name=channel_name,
        code=model.metric_code,
        unit=model.unit,
        params=_json_of(model.params_json),
        params_locked=bool(model.params_locked),
        enabled=bool(model.enabled),
    )


def _names(session: Session, task_id: int) -> dict[int, str]:
    """node_id → 通道名（指标节点也映射到它所属的通道名）。"""
    nodes = tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in nodes}
    names: dict[int, str] = {}
    for node in nodes:
        if node.node_type == "channel":
            names[node.id] = node.name
        elif node.node_type == "metric" and node.parent_id in by_id:
            names[node.id] = by_id[node.parent_id].name
    return names


def _active(session: Session, assign_id: int) -> MetricAssign:
    model = session.get(MetricAssign, assign_id)
    if model is None or model.deleted_at is not None:
        raise MetricAssignError(f"指标授予不存在：{assign_id}")
    return model


def list_assignments(session: Session, task_id: int) -> list[MetricRow]:
    names = _names(session, task_id)
    order = {node.id: index for index, node in enumerate(tree.list_nodes(session, task_id))}
    stmt = (
        select(MetricAssign)
        .where(MetricAssign.task_id == task_id, MetricAssign.deleted_at.is_(None))
        .order_by(MetricAssign.id)
    )
    models = list(session.execute(stmt).scalars())
    rows = [_to_row(model, names.get(model.node_id, "")) for model in models]
    rows.sort(key=lambda row: (order.get(row.node_id, 10**6), row.code))
    return rows


def _resolve_metric_node(session: Session, task_id: int, node_id: int, code: str) -> tree.NodeRow:
    """把「通道节点 或 指标节点」解析成**指标节点**（参数卡落点，马哥 2026-11-02）。

    参数属于指标，不属于通道：落点一律是指标节点。传通道节点时按指标码找它下面的
    指标节点；找不到说明指标还没装配，报错让用户先装配，不自动造节点。
    """
    node = tree.get_node(session, node_id)
    if node.node_type == "metric":
        own = (node.attrs or {}).get("code") or node.name
        if own != code:
            raise MetricAssignError(f"指标节点 {node.name} 对应 {own}，不能挂 {code}")
        return node
    if node.node_type != "channel":
        raise MetricAssignError(
            f"指标只能挂在指标节点上（通道节点或指标节点均可指定），"
            f"节点 {node_id} 是 {node.type_text}"
        )
    for channel, metric_node, metric_code in _metric_nodes_by_channel(session, task_id):
        if channel.id == node.id and metric_code == code:
            return metric_node
    raise MetricAssignError(
        f"通道 {node.name} 下没有 {code} 指标节点：先在该通道的「子节点」里装配指标，再绑参数"
    )


def _plugin_codes() -> dict:
    """自定义算法插件提供的指标码（导入失败就当没有，不影响内置流程）。"""
    try:
        from ..features import metric_plugins
    except ImportError:  # pragma: no cover - 循环导入保护
        return {}
    plugins, _errors = metric_plugins.load_plugins()
    return plugins


def assign_metric(
    session: Session,
    task_id: int,
    node_id: int,
    code: str,
    params: dict[str, Any] | None = None,
    unit: str | None = None,
    params_locked: bool = False,
) -> MetricRow:
    """给指标节点授予参数；同一指标节点同一指标只能授予一次。

    ``node_id`` 可以是通道节点（自动落到该通道对应指标节点）或指标节点本身。
    ``code`` 以指标台账为准：内置三指标总可装配，自建指标需先在台账里建好。
    """
    ledger = metric_def.find_metric(session, task_id, code)
    if code not in METRIC_CODES and ledger is None and code not in _plugin_codes():
        allowed = " / ".join(METRIC_CODES)
        raise MetricAssignError(
            f"指标码不在指标台账：{code}（内置 {allowed}；自建指标先去「指标配置」新增）"
        )

    node = _resolve_metric_node(session, task_id, node_id, code)
    node_id = node.id

    exists = session.execute(
        select(MetricAssign.id).where(
            MetricAssign.node_id == node_id,
            MetricAssign.metric_code == code,
            MetricAssign.deleted_at.is_(None),
        )
    ).first()
    if exists is not None:
        raise MetricAssignError(f"指标节点 {node.name} 已授予指标 {code}")

    default_unit = METRIC_CODES.get(code) or (ledger.unit if ledger else "")
    model = MetricAssign(
        task_id=task_id,
        node_id=node_id,
        metric_code=code,
        unit=unit or default_unit,
        params_json=json.dumps(params or {}, ensure_ascii=False),
        params_locked=1 if params_locked else 0,
        enabled=1,
    )
    session.add(model)
    session.commit()
    return _to_row(model, _names(session, task_id).get(node.id, node.name))


def find_assignment(session: Session, task_id: int, node_id: int, code: str) -> MetricAssign | None:
    """按通道节点或指标节点查绑定行（``node_id`` 两种语义都接受）。"""
    try:
        metric_node = _resolve_metric_node(session, task_id, node_id, code)
    except MetricAssignError:
        return None
    return (
        session.execute(
            select(MetricAssign).where(
                MetricAssign.node_id == metric_node.id,
                MetricAssign.metric_code == code,
                MetricAssign.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )


def _metric_nodes_by_channel(
    session: Session, task_id: int
) -> list[tuple[tree.NodeRow, tree.NodeRow, str]]:
    """树里所有「通道节点, 指标节点, 指标码」三元组（按树的节点顺序）。"""
    nodes = tree.list_nodes(session, task_id)
    channels = {node.id: node for node in nodes if node.node_type == "channel"}
    triples: list[tuple[tree.NodeRow, tree.NodeRow, str]] = []
    for node in nodes:
        if node.node_type != "metric" or node.parent_id not in channels:
            continue
        attrs = getattr(node, "attrs", None) or {}
        code = (attrs.get("code") or node.name) or node.name
        triples.append((channels[node.parent_id], node, code))
    return triples


def missing_bindings(session: Session, task_id: int) -> list[tuple[int, str, str]]:
    """还没绑参数的指标节点清单：(指标节点 id, 通道名, 指标码)。

    指标节点是早期数据/老模板导入留下的产物：树里有节点、``metric_assign`` 没行，
    界面显示「未绑定参数」，参数表单渲染不出来（改不了计算时长）。
    """
    bound = {(row.node_id, row.code) for row in list_assignments(session, task_id)}
    return [
        (metric_node.id, channel.name, code)
        for channel, metric_node, code in _metric_nodes_by_channel(session, task_id)
        if (metric_node.id, code) not in bound
    ]


def bind_missing(session: Session, task_id: int) -> tuple[int, list[str]]:
    """给缺绑定的「通道 × 指标」补上（空覆盖 ``{}`` = 跟随指标定义默认）。

    返回 ``(新增条数, 被跳过的指标码)``。指标码既不是内置三指标、台账里也没有的跳过，
    不中断整批；已有绑定的跳过，所以重复执行是幂等的。

    绑定行的 ``node_id`` 是**指标节点 id**（``assign_metric`` 的契约）。
    """
    created = 0
    skipped: list[str] = []
    for metric_node_id, _channel_name, code in missing_bindings(session, task_id):
        try:
            assign_metric(session, task_id, metric_node_id, code)
        except MetricAssignError:
            if code not in skipped:
                skipped.append(code)
            continue
        created += 1
    return created, skipped


def _definition_defaults(session: Session, task_id: int, code: str) -> dict[str, Any]:
    """该指标的「非节点」默认：硬默认 + 定义默认。"""
    ledger = metric_def.find_metric(session, task_id, code)
    return merge_params(default_params(code), ledger.defaults if ledger else {})


def definition_defaults(session: Session, task_id: int, code: str) -> dict[str, Any]:
    """公开口径：硬默认 + 指标定义默认（不含节点覆盖）。

    模板导入要算「跟随默认会得到什么」（S3 漂移清单）与「搬运到目标需要的 diff」，
    必须与计算层同一个函数，不另写一份。
    """
    return _definition_defaults(session, task_id, code)


def compose_params(
    code: str,
    definition_defaults: dict[str, Any] | None,
    override: dict[str, Any] | None,
    locked: bool = False,
) -> dict[str, Any]:
    """生效参数（纯函数，方案文档 3.1）。

    - ``locked=False``：硬默认 < 指标定义默认 < 节点覆盖项
    - ``locked=True``：``override`` 就是全量固化快照，定义改默认值不再影响本节点
    """
    if locked:
        return merge_params(default_params(code), override)
    return merge_params(default_params(code), definition_defaults, override)


def effective_params(session: Session, assign_id: int) -> dict[str, Any]:
    """某次授予的生效参数（计算层直接用它）。"""
    model = _active(session, assign_id)
    return compose_params(
        model.metric_code,
        _definition_defaults(session, model.task_id, model.metric_code),
        _json_of(model.params_json),
        bool(model.params_locked),
    )


def _effective_now(session: Session, model: MetricAssign) -> dict[str, Any]:
    """手上这行的生效参数（改之前 / 改之后各取一次，用来判断要不要置 stale）。"""
    return compose_params(
        model.metric_code,
        _definition_defaults(session, model.task_id, model.metric_code),
        _json_of(model.params_json),
        bool(model.params_locked),
    )


def _value_node_id(session: Session, model: MetricAssign) -> int:
    """指标值挂在**通道节点**上：参数卡在指标节点时，旧值按父通道节点标记 stale。"""
    node = tree.get_node(session, model.node_id)
    if node.node_type == "metric" and node.parent_id is not None:
        return node.parent_id
    return model.node_id


def _params_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """生效参数是否真的变了（数值按 float 比，1000 与 1000.0 算没变）。"""
    return bool(diff_params(left, right) or diff_params(right, left))


def update_params(session: Session, assign_id: int, params: dict[str, Any]) -> MetricRow:
    """写节点覆盖项（调用方负责只传 diff）；不改固化开关。

    生效参数真的变了 → 该 (node_id, metric_code) 的旧值置 ``stale=1``（本次只标记，不重算）。
    ``params_json`` 真正变化 → 写一条留痕（old / new 只含改动键，T3）。
    """
    model = _active(session, assign_id)
    before = _effective_now(session, model)
    before_override = _json_of(model.params_json)
    model.params_json = json.dumps(params or {}, ensure_ascii=False)
    note = f"{_names(session, model.task_id).get(model.node_id, '')} · {model.metric_code}"
    audit.record_param_diff(
        session,
        entity_type="metric_assign",
        entity_id=model.id,
        field="params_json",
        old_params=before_override,
        new_params=_json_of(model.params_json),
        actor=audit.actor_of(session, model.task_id),
        note=note,
    )
    session.commit()
    if _params_changed(before, _effective_now(session, model)):
        metric_store.mark_stale(session, _value_node_id(session, model), model.metric_code)
    return _to_row(model, _names(session, model.task_id).get(model.node_id, ""))


def set_locked(session: Session, assign_id: int, locked: bool) -> MetricRow:
    """固化/取消固化。

    固化：把当前全部生效值写进 ``params_json``（此后与定义默认脱钩）。
    取消固化：只留下与「硬默认 + 定义默认」真正不同的项，重新跟随定义。
    T3：``params_locked`` 与 ``params_json`` 的变化各留一条痕。
    """
    model = _active(session, assign_id)
    before = _effective_now(session, model)
    before_override = _json_of(model.params_json)
    before_locked = bool(model.params_locked)
    code = model.metric_code
    if locked:
        full = effective_params(session, assign_id)
        model.params_json = json.dumps(full, ensure_ascii=False)
        model.params_locked = 1
    else:
        full = effective_params(session, assign_id)
        base = _definition_defaults(session, model.task_id, code)
        model.params_json = json.dumps(diff_params(full, base), ensure_ascii=False)
        model.params_locked = 0
    note = f"{_names(session, model.task_id).get(model.node_id, '')} · {code}"
    actor = audit.actor_of(session, model.task_id)
    audit.record_param_diff(
        session,
        entity_type="metric_assign",
        entity_id=model.id,
        field="params_json",
        old_params=before_override,
        new_params=_json_of(model.params_json),
        actor=actor,
        note=note,
    )
    audit.record_change(
        session,
        entity_type="metric_assign",
        entity_id=model.id,
        field="params_locked",
        old_value=before_locked,
        new_value=bool(model.params_locked),
        actor=actor,
        note=note,
    )
    session.commit()
    if _params_changed(before, _effective_now(session, model)):
        metric_store.mark_stale(session, _value_node_id(session, model), model.metric_code)
    return _to_row(model, _names(session, model.task_id).get(model.node_id, ""))


def _channel_targets(nodes: list[tree.NodeRow], node_id: int) -> list[int]:
    """转速改在哪个节点上，就标记它（通道节点）或它下面各通道节点的旧值。"""
    by_id = {row.id: row for row in nodes}
    node = by_id.get(node_id)
    if node is None:
        return []
    if node.node_type == "channel":
        return [node.id]
    prefix = f"{node.path}/" if node.path else ""
    return [
        row.id
        for row in nodes
        if row.node_type == "channel" and row.path and row.path.startswith(prefix)
    ]


def mark_stale_for_definition_change(
    session: Session,
    task_id: int,
    codes: Sequence[str],
    before_defaults: dict[str, Any],
) -> tuple[int, int]:
    """指标定义默认值改了 → 把「跟着定义走」的节点旧值标 ``stale=1``。

    复查 v2 问题 3：``update_params`` / ``set_node_rpm`` 都会 ``mark_stale``，唯独改
    ``metric_def`` 的默认值没有——跟随定义的节点生效参数已经变了，历史值却还当有效值
    画在趋势上，用户看不出哪段作废。

    ``codes`` 是该定义在 ``metric_assign.metric_code`` 里可能出现的新/旧指标码（定义可以
    同时改名，授予行的码不一定跟着变）。

    口径：

    - 只处理 ``params_locked=0`` 的授予：固化行的参数是全量快照，定义默认值改了对它无影响；
    - 逐行比较「改默认前 / 改默认后」的**生效参数**：节点覆盖了被改的键 → 生效值没变，不标；
    - 只标记不重算，返回 ``(受影响节点数, 被标记的行数)``。
    """
    wanted = list(dict.fromkeys(code for code in codes if code))
    if not wanted:
        return 0, 0
    after = _definition_defaults(session, task_id, wanted[-1])
    before = merge_params(default_params(wanted[-1]), before_defaults)
    affected = 0
    marked = 0
    stmt = select(MetricAssign).where(
        MetricAssign.task_id == task_id,
        MetricAssign.metric_code.in_(wanted),
        MetricAssign.deleted_at.is_(None),
    )
    for model in session.execute(stmt).scalars():
        if model.params_locked:
            continue
        override = _json_of(model.params_json)
        if not _params_changed(
            compose_params(model.metric_code, before, override, False),
            compose_params(model.metric_code, after, override, False),
        ):
            continue
        affected += 1
        marked += metric_store.mark_stale(
            session, _value_node_id(session, model), model.metric_code
        )
    return affected, marked


def set_node_rpm(session: Session, node_id: int, rpm: float | None) -> int:
    """写节点手填转速（``rpm`` 为空/<=0 = 清空），转速基准变了就把相关旧值置 stale。

    返回被标记 stale 的行数；转速没变返回 0（不白标记）。
    改在监测对象节点上时，它下面各通道节点的值一并标记。
    """
    node = tree.get_node(session, node_id)
    attrs = dict(node.attrs)
    if rpm is not None and float(rpm) > 0:
        attrs[tree.RPM_ATTR] = f"{float(rpm):g}"
    else:
        attrs.pop(tree.RPM_ATTR, None)

    before = tree.node_rpm(tree.list_nodes(session, node.task_id), node_id)
    tree.update_node(session, node_id, attrs=attrs)
    nodes = tree.list_nodes(session, node.task_id)
    if before == tree.node_rpm(nodes, node_id):
        return 0
    marked = 0
    for channel_id in _channel_targets(nodes, node_id):
        marked += metric_store.mark_stale(session, channel_id)
    return marked


def unassign(session: Session, assign_id: int) -> None:
    model = _active(session, assign_id)
    model.deleted_at = datetime.now()
    session.commit()
