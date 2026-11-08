"""旧 ``threshold`` 表的历史读取接口（**历史遗留，不再写入、无人读取**）。

生效阈值（判级、继承）全在 ``store.limits`` 的 ``metric_limit`` 表上，本模块只是当年
「作用域就近覆盖」那一版阈值的遗留代码：表里可能还有历史行，所以读接口留着，
但**建任务不再播种**（G5，2026-11-01），也不要拿它判级。

需要新阈值请用 :mod:`lion_monitor.store.limits`。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Threshold

__all__ = ["list_thresholds", "resolve_threshold"]


def list_thresholds(session: Session, task_id: int) -> list[Threshold]:
    """任务下所有有效阈值，按作用域（通道优先）与指标排序。"""
    stmt = (
        select(Threshold)
        .where(Threshold.task_id == task_id, Threshold.deleted_at.is_(None))
        .order_by(Threshold.scope_type, Threshold.scope_code, Threshold.metric_code)
    )
    return list(session.execute(stmt).scalars())


def resolve_threshold(
    session: Session, task_id: int, metric_code: str, channel_code: str = ""
) -> Threshold | None:
    """就近覆盖：通道级 > 任务默认。没有就返回 None（表示未设阈值）。"""
    stmt = select(Threshold).where(
        Threshold.task_id == task_id,
        Threshold.metric_code == metric_code,
        Threshold.deleted_at.is_(None),
    )
    rows = list(session.execute(stmt).scalars())
    for row in rows:
        if row.scope_type == "channel" and row.scope_code == channel_code:
            return row
    for row in rows:
        if row.scope_type == "task":
            return row
    return None
