"""监测对象台账：独立主数据（monitor_object），健康树之后只做引用。

与 store.objects 的区别：objects 读的是健康树节点（旧模型），
本模块是台账（新模型），迁移完成后前者会改为读台账。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import tree
from .models import ChannelReg, MonitorObject

__all__ = [
    "OBJECT_TYPES",
    "ObjectRegistryError",
    "ObjectRow",
    "create_object",
    "delete_object",
    "list_objects",
    "set_rated_rpm",
    "sync_from_tree",
    "update_object",
]

#: 允许的对象类型（唯一事实来源；store.objects 直接引用，别再抄一份）
OBJECT_TYPES: tuple[str, ...] = (
    "轴承",
    "电机",
    "气缸",
    "辊",
    "泵",
    "风机",
    "齿轮",
    "轴",
    "行星齿轮",
)


class ObjectRegistryError(Exception):
    """监测对象台账业务错误。"""


@dataclass(frozen=True)
class ObjectRow:
    id: int
    task_id: int
    name: str
    obj_type: str
    part: str
    rated_rpm: float | None = None
    remark: str = ""


def _to_row(model: MonitorObject) -> ObjectRow:
    return ObjectRow(
        id=model.id,
        task_id=model.task_id,
        name=model.name,
        obj_type=model.obj_type,
        part=model.part,
        rated_rpm=model.rated_rpm,
        remark=model.remark,
    )


def _check_type(obj_type: str) -> str:
    value = (obj_type or "").strip()
    if value not in OBJECT_TYPES:
        allowed = " / ".join(OBJECT_TYPES)
        raise ObjectRegistryError(f"对象类型只能是 {allowed}，收到：{obj_type!r}")
    return value


def list_objects(session: Session, task_id: int) -> list[ObjectRow]:
    stmt = (
        select(MonitorObject)
        .where(MonitorObject.task_id == task_id, MonitorObject.deleted_at.is_(None))
        .order_by(MonitorObject.id)
    )
    return [_to_row(model) for model in session.execute(stmt).scalars()]


def _active(session: Session, object_id: int) -> MonitorObject:
    model = session.get(MonitorObject, object_id)
    if model is None or model.deleted_at is not None:
        raise ObjectRegistryError(f"监测对象不存在：{object_id}")
    return model


def create_object(
    session: Session,
    task_id: int,
    name: str,
    obj_type: str,
    part: str = "",
    remark: str = "",
) -> ObjectRow:
    clean = (name or "").strip()
    if not clean:
        raise ObjectRegistryError("对象名称不能为空")
    kind = _check_type(obj_type)
    exists = session.execute(
        select(MonitorObject.id).where(
            MonitorObject.task_id == task_id,
            MonitorObject.name == clean,
            MonitorObject.deleted_at.is_(None),
        )
    ).first()
    if exists is not None:
        raise ObjectRegistryError(f"监测对象已存在：{clean}")

    model = MonitorObject(
        task_id=task_id,
        name=clean,
        obj_type=kind,
        part=(part or "").strip(),
        remark=remark,
        created_at=datetime.now(),
    )
    session.add(model)
    session.commit()
    return _to_row(model)


def update_object(
    session: Session,
    object_id: int,
    name: str | None = None,
    obj_type: str | None = None,
    part: str | None = None,
    remark: str | None = None,
) -> ObjectRow:
    model = _active(session, object_id)
    renamed = None
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ObjectRegistryError("对象名称不能为空")
        model.name = clean
        renamed = clean
    if obj_type is not None:
        model.obj_type = _check_type(obj_type)
    if part is not None:
        model.part = part.strip()
    if remark is not None:
        model.remark = remark
    if renamed is not None:
        tree.sync_ref_name(session, "monitor_object", model.id, renamed)
    session.commit()
    return _to_row(model)


def delete_object(session: Session, object_id: int) -> int:
    """软删监测对象，并把它名下的通道**解绑回自由态**（``object_id=NULL``）。

    通道是资产，不随对象删除；返回被解绑的通道条数，供页面回显。
    """
    model = _active(session, object_id)
    released = session.scalars(
        select(ChannelReg).where(ChannelReg.object_id == model.id, ChannelReg.deleted_at.is_(None))
    ).all()
    for channel in released:
        channel.object_id = None
    model.deleted_at = datetime.now()
    session.commit()
    return len(released)


def set_rated_rpm(session: Session, object_id: int, rpm: float | None) -> ObjectRow:
    """写监测对象额定转速（转速三级里的 ``rated``）；``None`` / 0 视为清空。

    只做数值合法性校验（必须是有限正数），不猜默认值：没有额定转速就是没有。
    """
    model = _active(session, object_id)
    if rpm is None:
        model.rated_rpm = None
    else:
        value = float(rpm)
        if not math.isfinite(value) or value <= 0:
            raise ObjectRegistryError(f"额定转速必须是大于 0 的数值，收到：{rpm!r}")
        model.rated_rpm = value
    session.commit()
    return _to_row(model)


def sync_from_tree(session: Session, task_id: int) -> int:
    """把健康树里已有的监测对象节点导入台账（按名称去重，幂等）。"""
    existing = {row.name for row in list_objects(session, task_id)}
    created = 0
    for node in tree.list_nodes(session, task_id):
        if node.node_type != "component" or node.name in existing:
            continue
        kind = str(node.attrs.get("类型", "")).strip()
        if kind not in OBJECT_TYPES:
            kind = OBJECT_TYPES[0]
        session.add(
            MonitorObject(
                task_id=task_id,
                name=node.name,
                obj_type=kind,
                part=str(node.attrs.get("部位", "")).strip(),
                created_at=datetime.now(),
            )
        )
        existing.add(node.name)
        created += 1
    session.commit()
    return created
