"""测试任务模块：建任务、查任务、改任务、归档、软删除。

任务号规则：``TT-YYYYMMDD-NN``，同一天内按已有任务数递增。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Channel, Dataset, DeletionLog, MetricValue, TaskNode, TestTask

__all__ = [
    "TaskError",
    "TaskNotFoundError",
    "TaskRow",
    "VALID_STATUS",
    "archive_task",
    "create_task",
    "get_task",
    "get_task_by_code",
    "list_deletion_logs",
    "list_tasks",
    "next_task_code",
    "pick_task",
    "soft_delete_task",
    "task_usage",
    "update_task",
]

#: 任务允许的状态
VALID_STATUS = frozenset({"draft", "ready", "done", "archived"})


class TaskError(Exception):
    """任务相关的业务错误（参数不合法等）。"""


class TaskNotFoundError(TaskError):
    """任务不存在或已被软删除。"""


@dataclass(frozen=True, slots=True)
class TaskRow:
    """任务的只读视图（给界面用，脱离 ORM 生命周期）。"""

    id: int
    code: str
    name: str
    status: str
    operator: str
    remark: str
    created_at: datetime
    deleted_at: datetime | None

    @property
    def archived(self) -> bool:
        return self.status == "archived"

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


def _to_row(model: TestTask) -> TaskRow:
    return TaskRow(
        id=model.id,
        code=model.code,
        name=model.name,
        status=model.status,
        operator=model.operator,
        remark=model.remark,
        created_at=model.created_at,
        deleted_at=model.deleted_at,
    )


def next_task_code(session: Session, when: date | None = None) -> str:
    """生成任务号 TT-YYYYMMDD-NN（同日递增，含已删除任务，避免号复用）。"""
    day = when or date.today()
    prefix = f"TT-{day:%Y%m%d}-"
    used = session.execute(
        select(func.count()).select_from(TestTask).where(TestTask.code.like(f"{prefix}%"))
    ).scalar_one()
    return f"{prefix}{used + 1:02d}"


def create_task(
    session: Session,
    name: str,
    operator: str = "mark",
    remark: str = "",
    when: date | None = None,
) -> TaskRow:
    """建任务：校验名称、生成任务号、建根节点。

    旧 ``threshold`` 表（历史遗留）不再播种：生效阈值全在 ``metric_limit``（G5）。
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise TaskError("任务名称不能为空")

    model = TestTask(
        code=next_task_code(session, when),
        name=clean_name,
        status="draft",
        operator=operator,
        remark=remark,
        created_at=datetime.now(),
    )
    session.add(model)
    session.flush()

    # 每个任务都要有根节点，设备/监测对象才有地方挂
    root = TaskNode(task_id=model.id, parent_id=None, node_type="task", name=model.name, path="")
    session.add(root)
    session.flush()
    root.path = str(root.id)
    session.commit()
    return _to_row(model)


def list_tasks(session: Session, include_deleted: bool = False) -> list[TaskRow]:
    """任务列表，新建的在前；默认不含已删除。"""
    stmt = select(TestTask).order_by(TestTask.id.desc())
    if not include_deleted:
        stmt = stmt.where(TestTask.deleted_at.is_(None))
    return [_to_row(row) for row in session.execute(stmt).scalars()]


def get_task(session: Session, task_id: int) -> TaskRow:
    """按 id 取任务；已软删除视为不存在。"""
    model = session.get(TestTask, task_id)
    if model is None or model.deleted_at is not None:
        raise TaskNotFoundError(f"任务不存在：{task_id}")
    return _to_row(model)


def get_task_by_code(session: Session, code: str) -> TaskRow:
    """按任务号取任务；已软删除视为不存在。"""
    stmt = select(TestTask).where(TestTask.code == code, TestTask.deleted_at.is_(None))
    model = session.execute(stmt).scalar_one_or_none()
    if model is None:
        raise TaskNotFoundError(f"任务不存在：{code}")
    return _to_row(model)


def pick_task(rows: Sequence[TaskRow], wanted_id: int | None) -> TaskRow | None:
    """界面当前任务：指定的存在就用它，否则退回最新的一个（rows 已按新→旧排）。"""
    if not rows:
        return None
    for row in rows:
        if row.id == wanted_id:
            return row
    return rows[0]


def update_task(session: Session, task_id: int, **fields: str) -> TaskRow:
    """改任务：只允许 name / remark / status。"""
    model = session.get(TestTask, task_id)
    if model is None or model.deleted_at is not None:
        raise TaskNotFoundError(f"任务不存在：{task_id}")

    if "name" in fields:
        clean = (fields["name"] or "").strip()
        if not clean:
            raise TaskError("任务名称不能为空")
        model.name = clean
        root = (
            session.execute(
                select(TaskNode).where(
                    TaskNode.task_id == task_id,
                    TaskNode.node_type == "task",
                    TaskNode.deleted_at.is_(None),
                )
            )
            .scalars()
            .first()
        )
        if root is not None:
            root.name = clean
    if "remark" in fields:
        model.remark = fields["remark"] or ""
    if "status" in fields:
        status = fields["status"]
        if status not in VALID_STATUS:
            allowed = " / ".join(sorted(VALID_STATUS))
            raise TaskError(f"任务状态不合法：{status}（允许 {allowed}）")
        model.status = status

    session.commit()
    return _to_row(model)


def archive_task(session: Session, task_id: int) -> TaskRow:
    """归档任务（软删除之外的收起方式，数据仍在）。"""
    return update_task(session, task_id, status="archived")


def soft_delete_task(session: Session, task_id: int, operator: str = "mark") -> TaskRow:
    """软删除任务，并写一条 deletion_log。"""
    model = session.get(TestTask, task_id)
    if model is None or model.deleted_at is not None:
        raise TaskNotFoundError(f"任务不存在：{task_id}")

    model.deleted_at = datetime.now()
    session.add(
        DeletionLog(
            entity_type="test_task",
            entity_id=model.id,
            entity_name=f"{model.code} {model.name}",
            mode="soft",
            cascade_counts=json.dumps({"dataset": 0, "channel": 0, "metric_value": 0}),
            operator=operator,
        )
    )
    session.commit()
    return _to_row(model)


def list_deletion_logs(session: Session) -> list[DeletionLog]:
    """删除留痕，最新的在前。"""
    stmt = select(DeletionLog).order_by(DeletionLog.id.desc())
    return list(session.execute(stmt).scalars())


def task_usage(session: Session, task_id: int) -> dict[str, int]:
    """任务下有效数据量：数据集 / 通道 / 指标值（都不含已删除）。"""
    ds = (
        select(func.count())
        .select_from(Dataset)
        .where(Dataset.task_id == task_id, Dataset.deleted_at.is_(None))
    )
    ch = (
        select(func.count())
        .select_from(Channel)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id, Dataset.deleted_at.is_(None), Channel.deleted_at.is_(None)
        )
    )
    mv = (
        select(func.count())
        .select_from(MetricValue)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Dataset.deleted_at.is_(None),
            Channel.deleted_at.is_(None),
            MetricValue.deleted_at.is_(None),
        )
    )
    return {
        "dataset": session.execute(ds).scalar_one(),
        "channel": session.execute(ch).scalar_one(),
        "metric_value": session.execute(mv).scalar_one(),
    }
