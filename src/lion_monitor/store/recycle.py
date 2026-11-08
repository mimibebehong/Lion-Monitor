"""回收站：读 ``deletion_log`` 留痕 + 恢复软删除项 + 保留期清理。

留痕表 ``deletion_log`` 由各软删除入口写（任务见 ``store.tasks``、数据集见
``store.datasets``、健康树节点见 ``store.tree``）；本模块负责读留痕、按实体复原
``deleted_at``，以及按保留期（G6，默认 7 天）清理过期条目。

节点级回收站（G6）：``store.tree.soft_delete_node`` 在留痕 ``payload`` 里存子树快照
（节点属性 + 子节点 + 该子树的指标授权 / 阈值），本模块按快照恢复；过期后连同软删的
节点行 / 授权行 / 阈值行一起真删。

选型：GitHub 上有 ``globus/sqlalchemy_softdelete``、``flipbit03/sqlalchemy-easy-softdelete``
一类「透明软删 + 自动过滤」的 ORM 插件，但它们会把查询口径全部接管，而本项目已按
全表 ``deleted_at`` 显式过滤、且回收站要展示带级联计数与操作人的留痕（插件只管标记，
不留痕），引入还得改全部查询并多一个依赖 —— 故自研这一层薄封装。
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, and_, or_, select
from sqlalchemy.orm import Session

from . import tree
from .models import (
    Channel,
    ChannelReg,
    Dataset,
    DeletionLog,
    MetricAssign,
    MetricLimit,
    MetricValue,
    Sensor,
    TaskNode,
    TestTask,
)

_log = logging.getLogger(__name__)

__all__ = [
    "ENTITY_LABELS",
    "RECYCLE_RETENTION_DAYS",
    "RETENTION_ENV",
    "PurgeResult",
    "RecycleRow",
    "RestoreResult",
    "expiry_of",
    "list_recycle_items",
    "purge_expired",
    "purge_expired_logs",
    "restore_item",
    "retention_days",
]

#: 回收站保留天数（马哥 2026-11-01 定：7 天）
RECYCLE_RETENTION_DAYS = 7

#: 保留天数的环境变量覆盖（正整数；非法值回落默认 7 天）
RETENTION_ENV = "LION_RECYCLE_RETENTION_DAYS"

#: 留痕里的实体类型 → 界面中文名
ENTITY_LABELS = {"dataset": "数据集", "test_task": "任务", "task_node": "节点"}

#: 实体类型 → ORM 模型（未知类型视为不可恢复）
_MODELS = {"dataset": Dataset, "test_task": TestTask, "task_node": TaskNode}


def retention_days() -> int:
    """保留天数：常量 7，可用 ``LION_RECYCLE_RETENTION_DAYS`` 覆盖（非法值回落 7）。"""
    raw = (os.environ.get(RETENTION_ENV) or "").strip()
    if not raw:
        return RECYCLE_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return RECYCLE_RETENTION_DAYS
    return days if days > 0 else RECYCLE_RETENTION_DAYS


def expiry_of(moment: datetime | None = None) -> datetime:
    """某次删除的保留期到期时间（``moment`` 缺省为现在）。"""
    return (moment or datetime.now()) + timedelta(days=retention_days())


@dataclass(frozen=True, slots=True)
class RecycleRow:
    """回收站一行：留痕信息 + 实体当前的删除态。"""

    log_id: int
    entity_type: str
    entity_label: str
    entity_id: int
    entity_name: str
    operator: str
    deleted_at: datetime
    cascade: dict[str, int] = field(default_factory=dict)
    expires_at: datetime | None = None

    @property
    def remaining_days(self) -> int | None:
        """剩余保留天数（只对节点级留痕有意义；向上取整，过期后为 0）。"""
        if self.entity_type != "task_node":
            return None
        expires = self.expires_at or (self.deleted_at + timedelta(days=retention_days()))
        seconds = (expires - datetime.now()).total_seconds()
        return max(0, math.ceil(seconds / 86400))


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """恢复结果：``ok`` 为假时 ``message`` 是给界面看的提示，不抛异常。"""

    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """保留期清理结果（启动时写日志用）。"""

    logs: int = 0
    nodes: int = 0
    assigns: int = 0
    limits: int = 0


def _entity(session: Session, entity_type: str, entity_id: int):
    model = _MODELS.get(entity_type)
    return None if model is None else session.get(model, entity_id)


def _cascade(raw: str) -> dict[str, int]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int | float)}


def _payload(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def list_recycle_items(session: Session) -> list[RecycleRow]:
    """回收站列表：软删除留痕里、实体**当前仍处于删除态**的行，最新的在前。

    已恢复、彻底删除（mode=hard / restore）或实体已不存在的留痕不列出。
    """
    stmt = select(DeletionLog).where(DeletionLog.mode == "soft").order_by(DeletionLog.id.desc())
    rows: list[RecycleRow] = []
    for log in session.execute(stmt).scalars():
        entity = _entity(session, log.entity_type, log.entity_id)
        if entity is None or entity.deleted_at is None:
            continue
        rows.append(
            RecycleRow(
                log_id=log.id,
                entity_type=log.entity_type,
                entity_label=ENTITY_LABELS.get(log.entity_type, log.entity_type),
                entity_id=log.entity_id,
                entity_name=log.entity_name,
                operator=log.operator,
                deleted_at=log.created_at,
                cascade=_cascade(log.cascade_counts),
                expires_at=log.expires_at,
            )
        )
    return rows


# ---------------------------------------------------------------- 恢复：数据集 / 任务


def _restore_dataset(session: Session, dataset: Dataset) -> dict[str, int]:
    """恢复数据集：数据集 + 它的通道 + 这些通道下的指标值一并复原。"""
    dataset.deleted_at = None
    channels = list(
        session.execute(select(Channel).where(Channel.dataset_id == dataset.id)).scalars()
    )
    values = 0
    for channel in channels:
        channel.deleted_at = None
        points = list(
            session.execute(
                select(MetricValue).where(MetricValue.channel_id == channel.id)
            ).scalars()
        )
        for point in points:
            if point.deleted_at is not None:
                values += 1
            point.deleted_at = None
    return {"channel": len(channels), "metric_value": values}


# ---------------------------------------------------------------- 恢复：节点子树


def _flatten(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = [entry]
    for child in entry.get("children") or []:
        if isinstance(child, dict):
            out.extend(_flatten(child))
    return out


def _refresh_paths(session: Session, model: TaskNode, parent: TaskNode | None) -> None:
    """改了父子关系后重算本节点与整棵子树的 ``path``（祖先 id 串）。"""
    old = model.path or ""
    model.path = f"{parent.path}/{model.id}" if parent is not None else str(model.id)
    session.flush()
    if not old or old == model.path:
        return
    tail = session.execute(select(TaskNode).where(TaskNode.path.like(f"{old}/%"))).scalars()
    for child in list(tail):
        child.path = model.path + child.path[len(old) :]
    session.flush()


def _active_row_exists(session: Session, model, node_id: int, code: str, exclude_id: int) -> bool:
    stmt = select(model.id).where(
        model.node_id == node_id,
        model.metric_code == code,
        model.deleted_at.is_(None),
        model.id != exclude_id,
    )
    return session.execute(stmt).first() is not None


def _restore_assigns(
    session: Session, payload: dict[str, Any], id_map: dict[int, int]
) -> tuple[int, int]:
    restored = skipped = 0
    for row in payload.get("assigns") or []:
        if not isinstance(row, dict):
            continue
        node_id = id_map.get(int(row["node_id"]), int(row["node_id"]))
        node = session.get(TaskNode, node_id)
        if node is None or node.deleted_at is not None:
            skipped += 1
            continue
        model = session.get(MetricAssign, int(row["id"]))
        if model is not None:
            if _active_row_exists(session, MetricAssign, node_id, row["metric_code"], model.id):
                skipped += 1
                continue
            model.node_id = node_id
            model.deleted_at = None
        else:
            if _active_row_exists(session, MetricAssign, node_id, row["metric_code"], -1):
                skipped += 1
                continue
            session.add(
                MetricAssign(
                    task_id=int(row["task_id"]),
                    node_id=node_id,
                    metric_code=row["metric_code"],
                    unit=row.get("unit") or "",
                    params_json=row.get("params_json") or "{}",
                    params_locked=int(row.get("params_locked") or 0),
                    enabled=int(row.get("enabled", 1)),
                )
            )
        restored += 1
    return restored, skipped


def _restore_limits(
    session: Session, payload: dict[str, Any], id_map: dict[int, int]
) -> tuple[int, int]:
    restored = skipped = 0
    for row in payload.get("limits") or []:
        if not isinstance(row, dict):
            continue
        node_id = id_map.get(int(row["node_id"]), int(row["node_id"]))
        node = session.get(TaskNode, node_id)
        if node is None or node.deleted_at is not None:
            skipped += 1
            continue
        model = session.get(MetricLimit, int(row["id"]))
        if model is not None:
            if _active_row_exists(session, MetricLimit, node_id, row["metric_code"], model.id):
                skipped += 1
                continue
            model.node_id = node_id
            model.deleted_at = None
        else:
            if _active_row_exists(session, MetricLimit, node_id, row["metric_code"], -1):
                skipped += 1
                continue
            session.add(
                MetricLimit(
                    task_id=int(row["task_id"]),
                    node_id=node_id,
                    metric_code=row["metric_code"],
                    warn=float(row.get("warn") or 0.0),
                    alert=float(row.get("alert") or 0.0),
                    direction=row.get("direction") or ">",
                    enabled=int(row.get("enabled", 1)),
                    updated_at=datetime.now(),
                )
            )
        restored += 1
    return restored, skipped


def _restore_sensors(session: Session, payload: dict[str, Any], id_map: dict[int, int]) -> int:
    """把随节点一起软删的传感器恢复（节点 id 变了就跟着改挂）。"""
    task_id = int(payload["node"]["task_id"])
    count = 0
    for entry in payload.get("sensors") or []:
        old_node = entry.get("node_id")
        new_node = id_map.get(int(old_node), int(old_node)) if old_node is not None else None
        model = session.get(Sensor, int(entry["id"]))
        if model is None:
            model = Sensor(task_id=task_id)
            session.add(model)
        model.task_id = task_id
        model.node_id = new_node
        model.channel_code = entry.get("channel_code") or ""
        model.model = entry.get("model") or ""
        model.sensitivity = entry.get("sensitivity") or ""
        model.range_g = entry.get("range_g") or ""
        model.mounting = entry.get("mounting") or ""
        model.calibrated_on = entry.get("calibrated_on") or ""
        model.status = entry.get("status") or "启用"
        model.remark = entry.get("remark") or ""
        model.deleted_at = None
        count += 1
    return count


def _restore_channel_owner(session: Session, id_map: dict[int, int]) -> None:
    """恢复通道节点时把归属写回台账：父级是监测对象就重新归属，否则保持自由。"""
    for new_id in id_map.values():
        node = session.get(TaskNode, new_id)
        if node is None or node.node_type != "channel" or node.ref_type != "channel_reg":
            continue
        parent = session.get(TaskNode, node.parent_id) if node.parent_id else None
        owner = (
            parent.ref_id
            if parent is not None
            and parent.node_type == "component"
            and parent.ref_type == "monitor_object"
            else None
        )
        reg = session.get(ChannelReg, node.ref_id)
        if reg is None:
            continue
        reg.object_id = owner


def _restore_node(session: Session, log: DeletionLog) -> RestoreResult:
    """按留痕里的子树快照恢复节点（父节点不在时挂到任务根下并提示）。"""
    payload = _payload(log.payload)
    root_entry = payload.get("node")
    if not isinstance(root_entry, dict) or "id" not in root_entry:
        return RestoreResult(False, f"留痕缺少节点快照，无法恢复：{log.entity_name}")
    task_id = int(root_entry["task_id"])
    try:
        task_root: TaskNode | None = session.get(TaskNode, tree.root_node(session, task_id).id)
    except tree.TreeError:
        task_root = None

    id_map: dict[int, int] = {}
    reparented = ref_dropped = 0
    for entry in _flatten(root_entry):
        old_id = int(entry["id"])
        parent_old = entry.get("parent_id")
        if parent_old is None:
            parent_id: int | None = None
        elif int(parent_old) in id_map:
            parent_id = id_map[int(parent_old)]
        else:
            parent_model = session.get(TaskNode, int(parent_old))
            if parent_model is not None and parent_model.deleted_at is None:
                parent_id = parent_model.id
            elif task_root is not None and entry.get("node_type") != "task":
                parent_id = task_root.id
                reparented += 1
            else:
                return RestoreResult(
                    False, f"父节点已不存在且任务无根节点，无法恢复：{entry.get('name')}"
                )

        model = session.get(TaskNode, old_id)
        if model is not None:
            model.deleted_at = None
            if model.parent_id != parent_id:
                model.parent_id = parent_id
                parent = session.get(TaskNode, parent_id) if parent_id else None
                _refresh_paths(session, model, parent)
            id_map[old_id] = model.id
            continue

        try:
            created = tree.create_node(
                session,
                task_id,
                entry["node_type"],
                entry["name"],
                parent_id=parent_id,
                attrs=dict(entry.get("attrs") or {}),
                sort=int(entry.get("sort") or 0),
                ref_type=entry.get("ref_type") or "",
                ref_id=entry.get("ref_id"),
            )
        except tree.TreeError:
            # 引用的台账记录已被删：退一步按无引用恢复，恢复后可在页面上重新绑定
            created = tree.create_node(
                session,
                task_id,
                entry["node_type"],
                entry["name"],
                parent_id=parent_id,
                attrs=dict(entry.get("attrs") or {}),
                sort=int(entry.get("sort") or 0),
            )
            ref_dropped += 1
        id_map[old_id] = created.id

    assigns, skipped_a = _restore_assigns(session, payload, id_map)
    limits, skipped_l = _restore_limits(session, payload, id_map)
    restored_sensors = _restore_sensors(session, payload, id_map)
    _restore_channel_owner(session, id_map)
    session.add(
        DeletionLog(
            entity_type="task_node",
            entity_id=int(root_entry["id"]),
            entity_name=log.entity_name,
            mode="restore",
            cascade_counts=json.dumps(
                {
                    "node": len(id_map),
                    "assign": assigns,
                    "limit": limits,
                    "sensor": restored_sensors,
                },
                ensure_ascii=False,
            ),
            operator=log.operator or "system",
            payload=json.dumps({"restored_from": log.id}, ensure_ascii=False),
            created_at=datetime.now(),
        )
    )
    session.commit()

    notes: list[str] = []
    if reparented:
        notes.append("原父节点已不在，已恢复到任务根下")
    if skipped_a + skipped_l:
        notes.append(f"{skipped_a + skipped_l} 条授权/阈值与现有有效记录重复，保留现有记录")
    if ref_dropped:
        notes.append(f"{ref_dropped} 个节点引用的台账已不存在，按无引用恢复")
    detail = f"节点 {len(id_map)} / 指标授权 {assigns} / 阈值 {limits}"
    if restored_sensors:
        detail += f" / 传感器 {restored_sensors}"
    suffix = f"；{'；'.join(notes)}" if notes else ""
    return RestoreResult(True, f"已恢复：{log.entity_name}（{detail}）{suffix}")


def restore_item(session: Session, log_id: int) -> RestoreResult:
    """按留痕恢复一条软删除记录；任何异常情况都只返回 ``ok=False`` 的提示。"""
    log = session.get(DeletionLog, log_id)
    if log is None:
        return RestoreResult(False, f"留痕不存在：{log_id}")
    if log.mode != "soft":
        return RestoreResult(False, f"{log.entity_name} 是彻底删除留痕，不可恢复")
    entity = _entity(session, log.entity_type, log.entity_id)
    if entity is None:
        return RestoreResult(False, f"原记录已不存在：{log.entity_name}（id={log.entity_id}）")
    if entity.deleted_at is None:
        return RestoreResult(False, f"{log.entity_name} 已是有效状态，无需恢复")

    if log.entity_type == "dataset":
        counts = _restore_dataset(session, entity)
        detail = f"通道 {counts['channel']} / 指标值 {counts['metric_value']}"
    elif log.entity_type == "task_node":
        try:
            return _restore_node(session, log)
        except Exception as exc:  # noqa: BLE001 - 恢复失败只给界面提示，不抛给页面
            session.rollback()
            return RestoreResult(False, f"恢复失败：{log.entity_name}（{exc}）")
    else:
        entity.deleted_at = None
        detail = ENTITY_LABELS.get(log.entity_type, log.entity_type)
    session.commit()
    return RestoreResult(True, f"已恢复：{log.entity_name}（{detail}）")


# ---------------------------------------------------------------- 保留期清理


def _expired_stmt(cutoff: datetime):
    """过期的节点留痕：``expires_at`` 兜底按「删除时间 + 保留天数」算（老库没有该列值）。"""
    fallback = cutoff - timedelta(days=retention_days())
    return select(DeletionLog).where(
        DeletionLog.entity_type == "task_node",
        DeletionLog.mode == "soft",
        or_(
            and_(DeletionLog.expires_at.is_not(None), DeletionLog.expires_at < cutoff),
            and_(DeletionLog.expires_at.is_(None), DeletionLog.created_at < fallback),
        ),
    )


def purge_expired(session: Session, now: datetime | None = None) -> PurgeResult:
    """删掉过期的节点留痕，并真删对应仍处于软删态的节点行 / 授权行 / 阈值行。

    安全兜底：节点行若还有**未删的子节点**（子节点被单独恢复过），保留该行（不硬删），
    只清留痕——避免把活着的子树挂在已消失的父节点下。
    """
    cutoff = now or datetime.now()
    logs = list(session.execute(_expired_stmt(cutoff)).scalars())
    if not logs:
        return PurgeResult()

    candidates: set[int] = set()
    for log in logs:
        entry = _payload(log.payload).get("node")
        if isinstance(entry, dict):
            candidates.update(int(item["id"]) for item in _flatten(entry) if "id" in item)

    models = (
        {
            model.id: model
            for model in session.execute(
                select(TaskNode).where(TaskNode.id.in_(candidates))
            ).scalars()
        }
        if candidates
        else {}
    )
    alive_parents = {
        row
        for (row,) in session.execute(
            select(TaskNode.parent_id).where(
                TaskNode.deleted_at.is_(None), TaskNode.parent_id.is_not(None)
            )
        ).fetchall()
    }
    removable = [
        node_id
        for node_id, model in models.items()
        if model.deleted_at is not None and node_id not in alive_parents
    ]

    assigns = (
        list(
            session.execute(
                select(MetricAssign).where(
                    MetricAssign.node_id.in_(removable), MetricAssign.deleted_at.is_not(None)
                )
            ).scalars()
        )
        if removable
        else []
    )
    limits = (
        list(
            session.execute(
                select(MetricLimit).where(
                    MetricLimit.node_id.in_(removable), MetricLimit.deleted_at.is_not(None)
                )
            ).scalars()
        )
        if removable
        else []
    )
    for row in [*assigns, *limits]:
        session.delete(row)
    for node_id in removable:
        session.delete(models[node_id])
    for log in logs:
        session.delete(log)
    session.commit()

    result = PurgeResult(
        logs=len(logs), nodes=len(removable), assigns=len(assigns), limits=len(limits)
    )
    _log.info(
        "回收站保留期清理：过期留痕 %d 条，真删节点 %d 行、授权 %d 行、阈值 %d 行（保留 %d 天）",
        result.logs,
        result.nodes,
        result.assigns,
        result.limits,
        retention_days(),
    )
    return result


def purge_expired_logs(engine: Engine, now: datetime | None = None) -> PurgeResult:
    """启动路径用：自己开会话跑一次保留期清理。"""
    with Session(engine) as session:
        return purge_expired(session, now)
