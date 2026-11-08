"""阈值：按「节点 × 指标」存一行，读取时沿树向上继承（通道 > 设备 > 任务默认）。

方向口径与判级规则全部来自 :mod:`lion_monitor.limits_rules`（单一来源），本模块只做存取。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..limits_rules import LimitError, marker_of, validate_limit
from . import audit, tree
from .models import MetricLimit

__all__ = [
    "LimitError",
    "LimitRow",
    "delete_limit",
    "list_limits",
    "resolve_limit",
    "resolve_limits",
    "set_limit",
]

#: 继承各层在界面上的叫法
SOURCE_TEXT = {
    "metric": "指标级",
    "channel": "通道级",
    "component": "对象级",
    "device": "设备级",
    "task": "任务默认",
}


@dataclass(frozen=True)
class LimitRow:
    id: int
    task_id: int
    node_id: int
    node_name: str
    node_type: str
    metric_code: str
    warn: float
    alert: float
    direction: str
    source: str


def _to_row(model: MetricLimit, node: tree.NodeRow) -> LimitRow:
    return LimitRow(
        id=model.id,
        task_id=model.task_id,
        node_id=model.node_id,
        node_name=node.name,
        node_type=node.node_type,
        metric_code=model.metric_code,
        warn=float(model.warn),
        alert=float(model.alert),
        direction=marker_of(model.direction),
        source=SOURCE_TEXT.get(node.node_type, node.node_type),
    )


def list_limits(
    session: Session, task_id: int, nodes: list[tree.NodeRow] | None = None
) -> list[LimitRow]:
    """任务下所有未删阈值行（``nodes`` 可由调用方传入，省一次取树）。"""
    rows = nodes if nodes is not None else tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in rows}
    stmt = (
        select(MetricLimit)
        .where(MetricLimit.task_id == task_id, MetricLimit.deleted_at.is_(None))
        .order_by(MetricLimit.id)
    )
    return [
        _to_row(model, by_id[model.node_id])
        for model in session.execute(stmt).scalars()
        if model.node_id in by_id
    ]


def set_limit(
    session: Session,
    task_id: int,
    node_id: int,
    metric_code: str,
    warn: float,
    alert: float,
    direction: str = ">",
) -> LimitRow:
    """在节点上设阈值；同一节点同一指标再次设置视为修改。

    ``direction`` 接受 ``>`` / ``upper`` / ``<`` / ``lower``。
    **0 表示该档不判**（马哥 2026-11-04：可以只设 warn 或只设 alert）；
    两侧都有值时按方向校验大小关系（上限 warn < alert、下限 warn > alert），
    两侧都 0 抛 :class:`LimitError`。

    T3：warn / alert / 方向**真正变化**的字段各写一条留痕（``change_log``），
    口径见 :mod:`lion_monitor.store.audit`。
    """
    validate_limit(direction, warn, alert)
    node = tree.get_node(session, node_id)
    if node.task_id != task_id:
        raise LimitError("节点不属于该任务")

    model = (
        session.execute(
            select(MetricLimit).where(
                MetricLimit.node_id == node_id,
                MetricLimit.metric_code == metric_code,
                MetricLimit.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )
    actor = audit.actor_of(session, task_id)
    note = f"{_channel_name(session, node)} · {metric_code}"
    before: dict[str, object] = {}
    if model is None:
        model = MetricLimit(task_id=task_id, node_id=node_id, metric_code=metric_code)
        session.add(model)
        session.flush()  # 先拿到 id，留痕才能指回这一行
    else:
        before = {
            "warn": float(model.warn),
            "alert": float(model.alert),
            "direction": marker_of(model.direction),
        }
    model.warn = float(warn)
    model.alert = float(alert)
    model.direction = marker_of(direction)
    model.enabled = 1
    model.updated_at = datetime.now()
    after = {"warn": float(warn), "alert": float(alert), "direction": marker_of(direction)}
    audit.record_fields(
        session,
        entity_type="metric_limit",
        entity_id=model.id,
        before=before,
        after=after,
        actor=actor,
        note=note,
    )
    session.commit()
    return _to_row(model, node)


def _channel_name(session: Session, node: tree.NodeRow) -> str:
    """留痕里的对象名：阈值挂在指标节点上，给它上一级（通道）的名字更好认。"""
    if node.parent_id is None:
        return node.name
    try:
        return tree.get_node(session, node.parent_id).name
    except tree.NodeNotFoundError:  # 父节点已删也不该挡住写阈值
        return node.name


def resolve_limit(
    session: Session, task_id: int, node_id: int, metric_code: str
) -> tuple[float, float, str, str] | None:
    """沿树向上找第一个命中的阈值：返回 (warn, alert, direction, 来源)。"""
    chain = tree.path_nodes(session, node_id)
    for node in reversed(chain):
        model = (
            session.execute(
                select(MetricLimit).where(
                    MetricLimit.node_id == node.id,
                    MetricLimit.metric_code == metric_code,
                    MetricLimit.deleted_at.is_(None),
                    MetricLimit.enabled == 1,
                )
            )
            .scalars()
            .first()
        )
        if model is not None:
            return (
                float(model.warn),
                float(model.alert),
                marker_of(model.direction),
                SOURCE_TEXT.get(node.node_type, node.node_type),
            )
    return None


def resolve_limits(
    session: Session,
    task_id: int,
    codes_by_node: dict[int, str],
    nodes: list[tree.NodeRow] | None = None,
) -> dict[int, tuple[float, float, str, str] | None]:
    """批量求每个节点的生效阈值（一次取全量阈值行 + 内存沿父链继承）。

    口径与 :func:`resolve_limit` 一致：从节点自己往上找第一个命中的阈值行（只看启用未删的），
    但**不按节点查祖先链**（原实现每个节点 2 次查询，树页 / 工作台因此 N+1）。
    """
    if not codes_by_node:
        return {}
    rows = nodes if nodes is not None else tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in rows}
    stmt = select(MetricLimit).where(
        MetricLimit.task_id == task_id,
        MetricLimit.deleted_at.is_(None),
        MetricLimit.enabled == 1,
    )
    index = {(model.node_id, model.metric_code): model for model in session.execute(stmt).scalars()}

    found: dict[int, tuple[float, float, str, str] | None] = {}
    for node_id, metric_code in codes_by_node.items():
        node = by_id.get(node_id)
        hit = None
        while node is not None:
            model = index.get((node.id, metric_code))
            if model is not None:
                hit = (
                    float(model.warn),
                    float(model.alert),
                    marker_of(model.direction),
                    SOURCE_TEXT.get(node.node_type, node.node_type),
                )
                break
            node = by_id.get(node.parent_id) if node.parent_id else None
        found[node_id] = hit
    return found


def delete_limit(session: Session, limit_id: int) -> None:
    model = session.get(MetricLimit, limit_id)
    if model is None or model.deleted_at is not None:
        raise LimitError(f"阈值不存在：{limit_id}")
    model.deleted_at = datetime.now()
    session.commit()
