"""通道台账：通道号挂在监测对象台账下（一对象多通道）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import tree
from .models import ChannelReg, MonitorObject

__all__ = [
    "ChannelError",
    "ChannelRow",
    "available_for_object",
    "create_channel",
    "delete_channel",
    "list_channels",
    "set_owner",
    "sync_from_tree",
    "update_channel",
]


class ChannelError(Exception):
    """通道台账业务错误。"""


@dataclass(frozen=True)
class ChannelRow:
    id: int
    task_id: int
    #: 为空 = 自由通道（不归属任何监测对象）
    object_id: int | None
    code: str
    object_name: str
    remark: str = ""

    @property
    def is_free(self) -> bool:
        return self.object_id is None


def _to_row(model: ChannelReg, object_name: str) -> ChannelRow:
    return ChannelRow(
        id=model.id,
        task_id=model.task_id,
        object_id=model.object_id,
        code=model.code,
        object_name=object_name,
        remark=model.remark,
    )


def _object_names(session: Session) -> dict[int, str]:
    return {
        row.id: row.name
        for row in session.execute(select(MonitorObject)).scalars()
        if row.deleted_at is None
    }


def list_channels(session: Session, task_id: int) -> list[ChannelRow]:
    names = _object_names(session)
    stmt = (
        select(ChannelReg)
        .where(ChannelReg.task_id == task_id, ChannelReg.deleted_at.is_(None))
        .order_by(ChannelReg.id)
    )
    return [
        _to_row(model, names.get(model.object_id, "")) for model in session.execute(stmt).scalars()
    ]


def _active(session: Session, channel_id: int) -> ChannelReg:
    model = session.get(ChannelReg, channel_id)
    if model is None or model.deleted_at is not None:
        raise ChannelError(f"通道不存在：{channel_id}")
    return model


def create_channel(
    session: Session, task_id: int, object_id: int | None, code: str, remark: str = ""
) -> ChannelRow:
    """建通道；``object_id=None`` 表示自由通道（不归属任何监测对象）。"""
    clean = (code or "").strip()
    if not clean:
        raise ChannelError("通道号不能为空")
    obj_name = ""
    if object_id is not None:
        obj = session.get(MonitorObject, object_id)
        if obj is None or obj.deleted_at is not None:
            raise ChannelError(f"监测对象不存在：{object_id}")
        obj_name = obj.name
    exists = session.execute(
        select(ChannelReg.id).where(
            ChannelReg.task_id == task_id,
            ChannelReg.code == clean,
            ChannelReg.deleted_at.is_(None),
        )
    ).first()
    if exists is not None:
        raise ChannelError(f"通道已存在：{clean}")

    model = ChannelReg(
        task_id=task_id,
        object_id=object_id,
        code=clean,
        remark=remark,
        created_at=datetime.now(),
    )
    session.add(model)
    session.commit()
    return _to_row(model, obj_name)


def set_owner(session: Session, channel_id: int, object_id: int | None) -> ChannelRow:
    """绑定/改绑/解绑通道归属：``object_id=None`` = 解绑成自由通道。"""
    model = _active(session, channel_id)
    name = ""
    if object_id is not None:
        obj = session.get(MonitorObject, object_id)
        if obj is None or obj.deleted_at is not None:
            raise ChannelError(f"监测对象不存在：{object_id}")
        name = obj.name
    model.object_id = object_id
    session.commit()
    return _to_row(model, name)


def update_channel(
    session: Session, channel_id: int, code: str | None = None, remark: str | None = None
) -> ChannelRow:
    model = _active(session, channel_id)
    renamed = None
    if code is not None:
        clean = code.strip()
        if not clean:
            raise ChannelError("通道号不能为空")
        model.code = clean
        renamed = clean
    if remark is not None:
        model.remark = remark
    if renamed is not None:
        tree.sync_ref_name(session, "channel_reg", model.id, renamed)
    session.commit()
    return _to_row(model, _object_names(session).get(model.object_id, ""))


def delete_channel(session: Session, channel_id: int) -> None:
    model = _active(session, channel_id)
    model.deleted_at = datetime.now()
    session.commit()


def sync_from_tree(session: Session, task_id: int) -> int:
    """把健康树里的通道节点导入台账（按对象名+通道名去重，幂等）。"""
    from . import object_registry

    object_registry.sync_from_tree(session, task_id)  # 通道要挂在对象上，先补对象台账
    objects = {row.name: row.id for row in list_objects_of(session, task_id)}
    existing = {(row.object_name, row.code) for row in list_channels(session, task_id)}
    created = 0
    nodes = {node.id: node for node in tree.list_nodes(session, task_id)}
    for node in nodes.values():
        if node.node_type != "channel":
            continue
        parent = nodes.get(node.parent_id or 0)
        if parent is None:
            continue
        object_id = objects.get(parent.name)
        if object_id is None or (parent.name, node.name) in existing:
            continue
        session.add(
            ChannelReg(
                task_id=task_id,
                object_id=object_id,
                code=node.name,
                created_at=datetime.now(),
            )
        )
        existing.add((parent.name, node.name))
        created += 1
    session.commit()
    return created


def available_for_object(
    rows: list[ChannelRow], object_id: int | None, used: set[str] | frozenset[str] = frozenset()
) -> list[ChannelRow]:
    """可装配到 object_id 名下的通道：未被占用，且台账上就属于这个对象。

    ``object_id=None`` 时不按对象过滤（只去重）。健康树「新增通道」用它，
    避免把别的对象的通道（还顶着别人的对象名）列到当前对象下面。
    """
    pool = [row for row in rows if row.code not in used]
    if object_id is None:
        return pool
    return [row for row in pool if row.object_id == object_id]


def list_objects_of(session: Session, task_id: int):
    """本任务下未删的监测对象（避免循环 import）。"""
    from . import object_registry

    return object_registry.list_objects(session, task_id)


def sync_ownership(session: Session, task_id: int) -> int:
    """让通道台账的归属跟随健康树装配（树是唯一真相）。

    - 通道节点挂在哪个监测对象下，``object_id`` 就指向那个对象；
    - 挂在多个对象下时保留当前值（不在其中则取第一个）；
    - 树上已无该通道节点 → 置回自由态（object_id=NULL）。

    2026-11-02：健康树上配置好了之后，台账里的装配关系要跟着更新。
    """
    from .models import TaskNode

    nodes = {
        row.id: row
        for row in session.execute(
            select(TaskNode).where(TaskNode.task_id == task_id, TaskNode.deleted_at.is_(None))
        ).scalars()
    }
    owners: dict[int, list[int]] = {}
    for row in nodes.values():
        if row.node_type != "channel" or row.ref_type != "channel_reg" or not row.ref_id:
            continue
        parent = nodes.get(row.parent_id) if row.parent_id else None
        if parent is not None and parent.ref_type == "monitor_object" and parent.ref_id:
            owners.setdefault(row.ref_id, []).append(parent.ref_id)

    changed = 0
    regs = session.execute(
        select(ChannelReg).where(ChannelReg.task_id == task_id, ChannelReg.deleted_at.is_(None))
    ).scalars()
    for reg in regs:
        options = owners.get(reg.id) or []
        desired = (
            None if not options else (reg.object_id if reg.object_id in options else options[0])
        )
        if reg.object_id != desired:
            reg.object_id = desired
            changed += 1
    return changed


def release_unassembled(session: Session, task_id: int) -> int:
    """兼容旧调用：等价于按装配关系同步归属。"""
    return sync_ownership(session, task_id)
