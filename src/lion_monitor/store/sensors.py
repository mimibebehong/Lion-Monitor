"""传感器台账：一个通道一台传感器（型号/灵敏度/量程/安装方式/标定日期/状态）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import channel_reg, tree
from .models import Sensor, TaskNode, TestTask

__all__ = [
    "SENSOR_STATUS",
    "SensorError",
    "SensorRow",
    "attach_node",
    "bind_object",
    "create_sensor",
    "delete_sensor",
    "get_sensor",
    "list_sensors",
    "sensor_of_node",
    "update_sensor",
]

#: 传感器状态
SENSOR_STATUS: tuple[str, ...] = ("启用", "停用", "已拆")


class SensorError(Exception):
    """传感器台账业务错误。"""


@dataclass(frozen=True)
class SensorRow:
    id: int
    task_id: int
    node_id: int | None
    channel_name: str
    channel_code: str
    model: str
    sensitivity: str
    range_g: str
    mounting: str
    calibrated_on: str
    status: str
    remark: str = ""
    #: 归属是绑定关系：为空 = 自由传感器（不归属任何监测对象）
    object_id: int | None = None
    object_name: str = ""

    @property
    def is_free(self) -> bool:
        return self.object_id is None


def _to_row(
    model: Sensor, channel_name: str, ownership: tuple[int | None, str] = (None, "")
) -> SensorRow:
    object_id, object_name = ownership
    return SensorRow(
        id=model.id,
        task_id=model.task_id,
        node_id=model.node_id,
        channel_name=channel_name or model.channel_code or "未分配",
        channel_code=model.channel_code,
        model=model.model,
        sensitivity=model.sensitivity,
        range_g=model.range_g,
        mounting=model.mounting,
        calibrated_on=model.calibrated_on,
        status=model.status,
        remark=model.remark,
        object_id=object_id,
        object_name=object_name,
    )


def _ownership(session: Session, task_id: int) -> dict[str, tuple[int | None, str]]:
    """通道号 → (归属对象 id, 对象名)；通道号来自通道台账，空对象 id 表示自由。"""
    return {
        row.code: (row.object_id, row.object_name)
        for row in channel_reg.list_channels(session, task_id)
    }


def models_by_channel(session: Session, task_id: int) -> dict[int, str]:
    """通道台账行 id → 传感器型号。

    按「传感器挂在哪个通道节点」解析（``sensor.node_id`` → 节点的 ``ref_id``），
    文本通道号只作兜底；否则老数据 channel_code 为空时会被误判成「未挂传感器」。
    """
    from .channel_reg import list_channels  # 延迟 import：channel_reg 反过来依赖 tree

    regs = {row.id: row for row in list_channels(session, task_id)}
    by_code = {row.code: row.id for row in regs.values()}
    nodes = {
        node.id: node
        for node in session.execute(
            select(TaskNode).where(TaskNode.task_id == task_id, TaskNode.deleted_at.is_(None))
        ).scalars()
    }
    out: dict[int, str] = {}
    rows = session.execute(
        select(Sensor)
        .where(Sensor.task_id == task_id, Sensor.deleted_at.is_(None))
        .order_by(Sensor.id)
    ).scalars()
    for row in rows:
        if not row.model:
            continue
        node = nodes.get(row.node_id) if row.node_id else None
        reg_id: int | None = None
        if node is not None and node.ref_type == "channel_reg" and node.ref_id in regs:
            reg_id = node.ref_id
        elif row.channel_code and by_code.get(row.channel_code) is not None:
            reg_id = by_code[row.channel_code]
        elif node is not None:
            reg_id = by_code.get(node.name)
        if reg_id is not None and reg_id not in out:
            out[reg_id] = row.model
    return out


def _channel_names(session: Session, task_id: int) -> dict[int, str]:
    stmt = select(TaskNode.id, TaskNode.name).where(
        TaskNode.task_id == task_id, TaskNode.deleted_at.is_(None)
    )
    return {node_id: name for node_id, name in session.execute(stmt)}


def _active_model(session: Session, sensor_id: int) -> Sensor:
    model = session.get(Sensor, sensor_id)
    if model is None or model.deleted_at is not None:
        raise SensorError(f"传感器不存在：{sensor_id}")
    return model


def list_sensors(session: Session, task_id: int) -> list[SensorRow]:
    """任务下全部传感器（按通道在树中的顺序）。"""
    names = _channel_names(session, task_id)
    owners = _ownership(session, task_id)
    order = {node.id: index for index, node in enumerate(tree.list_nodes(session, task_id))}
    stmt = (
        select(Sensor)
        .where(Sensor.task_id == task_id, Sensor.deleted_at.is_(None))
        .order_by(Sensor.id)
    )
    rows = [
        _to_row(
            model,
            names.get(model.node_id, ""),
            owners.get(_code_of(model, names.get(model.node_id, "")), (None, "")),
        )
        for model in session.execute(stmt).scalars()
    ]
    rows.sort(key=lambda row: order.get(row.node_id, 10**6))
    return rows


def _code_of(model: Sensor, node_name: str = "") -> str:
    """传感器对应的通道号：优先自身字段，其次所属通道节点的名字。"""
    return (model.channel_code or "").strip() or (node_name or "").strip()


def _component_node(session: Session, task_id: int, object_id: int) -> TaskNode | None:
    return (
        session.execute(
            select(TaskNode).where(
                TaskNode.task_id == task_id,
                TaskNode.node_type == "component",
                TaskNode.ref_type == "monitor_object",
                TaskNode.ref_id == object_id,
                TaskNode.deleted_at.is_(None),
            )
        )
        .scalars()
        .first()
    )


def attach_node(session: Session, sensor_id: int, node_id: int) -> SensorRow:
    """把传感器挂到通道节点上（健康树装配完成后的绑定动作）。"""
    model = _active_model(session, sensor_id)
    node = tree.get_node(session, node_id)
    if node.node_type != "channel":
        raise SensorError(f"传感器只能挂在通道上，节点 {node_id} 是 {node.type_text}")
    model.node_id = node.id
    model.channel_code = node.name
    session.commit()
    return get_sensor(session, sensor_id)


def bind_object(session: Session, sensor_id: int, object_id: int | None) -> SensorRow:
    """绑定 / 改绑 / 解绑传感器的归属对象（``None`` = 解绑成自由）。

    - 已装配到健康树的：改绑会把通道节点挪到目标对象下（目标对象未装配则报错）；
      解绑会把通道节点摘进回收站（节点上的指标授权/阈值一起走），传感器留在台账当自由态。
    - 未装配的（自由）：只改台账归属，装配仍在健康树里显式操作。
    """
    model = _active_model(session, sensor_id)
    task_id = model.task_id
    names = _channel_names(session, task_id)
    node = None
    if model.node_id is not None:
        node = session.get(TaskNode, model.node_id)
        if node is not None and node.deleted_at is not None:
            node = None
    code = _code_of(model, names.get(model.node_id, ""))

    if object_id is not None:
        if session.get(TestTask, task_id) is None:
            raise SensorError("任务不存在")
        target = _component_node(session, task_id, object_id)
        if node is not None and target is None:
            raise SensorError("目标监测对象还没装配到健康树，先在健康树里把它加上再改绑")

    if node is not None:
        if object_id is None:
            tree.soft_delete_node(session, node.id, cascade_sensors=False)
            model.node_id = None
        else:
            parent = session.get(TaskNode, node.parent_id) if node.parent_id else None
            current = (
                parent.ref_id
                if parent is not None and parent.ref_type == "monitor_object"
                else None
            )
            target = _component_node(session, task_id, object_id)
            if current != object_id and target is not None:
                tree.reparent_node(session, node.id, target.id)

    if not code:
        raise SensorError("传感器没有通道号，无法设置归属")
    model.channel_code = code
    existing = next(
        (row for row in channel_reg.list_channels(session, task_id) if row.code == code), None
    )
    if existing is None:
        channel_reg.create_channel(session, task_id, object_id, code)
    else:
        channel_reg.set_owner(session, existing.id, object_id)
    session.commit()
    return get_sensor(session, sensor_id)


def sensor_of_node(session: Session, node_id: int) -> SensorRow | None:
    """通道节点上那台在用的传感器（优先「启用」，否则最新一台）；没有则 None。

    健康树「通道属性」只读展示用它：和「传感器」页同一个来源，不另存一份。
    """
    rows = list(
        session.execute(
            select(Sensor)
            .where(Sensor.node_id == node_id, Sensor.deleted_at.is_(None))
            .order_by(Sensor.id)
        ).scalars()
    )
    if not rows:
        # 同一通道（台账同一行）可能装配在多个监测对象下：
        # 先按 channel_reg 找兄弟通道节点共用同一台传感器，再按通道号兜底
        try:
            node = tree.get_node(session, node_id)
        except tree.NodeNotFoundError:
            return None
        if node.node_type == "channel":
            if node.ref_type == "channel_reg" and node.ref_id is not None:
                siblings = [
                    item.id
                    for item in tree.list_nodes(session, node.task_id)
                    if item.node_type == "channel"
                    and item.ref_type == "channel_reg"
                    and item.ref_id == node.ref_id
                ]
                if siblings:
                    rows = list(
                        session.execute(
                            select(Sensor)
                            .where(Sensor.node_id.in_(siblings), Sensor.deleted_at.is_(None))
                            .order_by(Sensor.id)
                        ).scalars()
                    )
            if not rows and node.name:
                rows = list(
                    session.execute(
                        select(Sensor)
                        .where(Sensor.channel_code == node.name, Sensor.deleted_at.is_(None))
                        .order_by(Sensor.id)
                    ).scalars()
                )
    if not rows:
        return None
    chosen = next((row for row in rows if row.status == "启用"), rows[-1])
    return _to_row(chosen, "")


def get_sensor(session: Session, sensor_id: int) -> SensorRow:
    model = _active_model(session, sensor_id)
    names = _channel_names(session, model.task_id)
    code = _code_of(model, names.get(model.node_id, ""))
    return _to_row(
        model,
        names.get(model.node_id, ""),
        _ownership(session, model.task_id).get(code, (None, "")),
    )


def create_sensor(
    session: Session,
    task_id: int,
    node_id: int,
    model: str = "",
    sensitivity: str = "",
    range_g: str = "",
    mounting: str = "",
    calibrated_on: str = "",
    status: str = "启用",
    remark: str = "",
    channel_code: str = "",
) -> SensorRow:
    """给通道挂一台传感器；一个通道只能有一台在用。"""
    node_name = ""
    code = (channel_code or "").strip()
    if code and node_id is None:
        # 手输通道号：能对上现有通道就自动关联，否则只存通道号
        for item in tree.list_nodes(session, task_id):
            if item.node_type == "channel" and item.name == code:
                node_id = item.id
                break
    if node_id is not None:
        node = tree.get_node(session, node_id)
        if node.node_type != "channel":
            raise SensorError(f"传感器只能挂在通道上，节点 {node_id} 是 {node.type_text}")
        if node.task_id != task_id:
            raise SensorError("通道不属于该任务")
        node_name = node.name
    if not (model or "").strip():
        raise SensorError("传感器型号不能为空")
    if status not in SENSOR_STATUS:
        allowed = " / ".join(SENSOR_STATUS)
        raise SensorError(f"传感器状态不合法：{status}（允许 {allowed}）")

    # 同一通道可留多台历史传感器，但只允许一台在用：新建时把旧的置停止
    if node_id is not None:
        previous = (
            session.execute(
                select(Sensor).where(
                    Sensor.node_id == node_id,
                    Sensor.deleted_at.is_(None),
                    Sensor.status == "启用",
                )
            )
            .scalars()
            .all()
        )
        for item in previous:
            item.status = "停用"

    row = Sensor(
        task_id=task_id,
        node_id=node_id,
        channel_code=code,
        model=model.strip(),
        sensitivity=sensitivity.strip(),
        range_g=range_g.strip(),
        mounting=mounting.strip(),
        calibrated_on=calibrated_on.strip(),
        status=status,
        remark=remark,
        created_at=datetime.now(),
    )
    session.add(row)
    session.commit()
    return _to_row(row, node_name)


def update_sensor(session: Session, sensor_id: int, **fields: str) -> SensorRow:
    model = _active_model(session, sensor_id)
    if "status" in fields and fields["status"] not in SENSOR_STATUS:
        allowed = " / ".join(SENSOR_STATUS)
        raise SensorError(f"传感器状态不合法：{fields['status']}（允许 {allowed}）")
    if "model" in fields:
        if not (fields["model"] or "").strip():
            raise SensorError("传感器型号不能为空")
        model.model = fields["model"].strip()
    if "node_id" in fields:
        target = fields["node_id"]
        if target is None:
            model.node_id = None
        else:
            node = tree.get_node(session, int(target))
            if node.node_type != "channel":
                raise SensorError(f"传感器只能挂在通道上，节点 {target} 是 {node.type_text}")
            if node.task_id != model.task_id:
                raise SensorError("通道不属于该任务")
            model.node_id = node.id
    if "channel_code" in fields:
        code = (fields["channel_code"] or "").strip()
        model.channel_code = code
        if code:
            match = next(
                (
                    node
                    for node in tree.list_nodes(session, model.task_id)
                    if node.node_type == "channel" and node.name == code
                ),
                None,
            )
            model.node_id = match.id if match else None
    for key in ("sensitivity", "range_g", "mounting", "calibrated_on", "status", "remark"):
        if key in fields:
            setattr(model, key, (fields[key] or "").strip())
    session.commit()
    return _to_row(model, _channel_names(session, model.task_id).get(model.node_id, ""))


def delete_sensor(session: Session, sensor_id: int) -> None:
    model = _active_model(session, sensor_id)
    model.deleted_at = datetime.now()
    task = session.get(TestTask, model.task_id)
    if task is not None:
        task.updated_at = datetime.now()
    session.commit()
