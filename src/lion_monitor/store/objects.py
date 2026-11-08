"""监测对象台账：task_node 里组件节点的读写视图。

监测对象不单独建表——它就是健康树里 node_type=component 的节点，
台账（类型/部位/通道数/指标数）由节点属性与子树现算，避免同一实体存两份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from . import tree
from .object_registry import OBJECT_TYPES
from .tree import NodeRow

__all__ = [
    "OBJECT_TYPES",
    "ObjectError",
    "ObjectRow",
    "create_object",
    "delete_object",
    "get_object",
    "list_objects",
    "update_object",
]

#: 允许的监测对象类型（界面下拉用）：与 object_registry 共用同一份定义


class ObjectError(Exception):
    """监测对象业务错误（类型不合法等）。"""


@dataclass(frozen=True)
class ObjectRow:
    """监测对象台账行。"""

    node_id: int
    name: str
    obj_type: str
    part: str
    device_name: str
    channel_count: int
    metric_count: int
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def channel_names(self) -> list[str]:
        return list(self.attrs.get("_channels", []))


def _check_type(obj_type: str) -> str:
    value = (obj_type or "").strip()
    if value not in OBJECT_TYPES:
        allowed = " / ".join(OBJECT_TYPES)
        raise ObjectError(f"对象类型只能是 {allowed}，收到：{obj_type!r}")
    return value


def _build_row(node: NodeRow, nodes: list[NodeRow], device_name: str) -> ObjectRow:
    """由全量节点算出台账行（通道数、指标数按子树统计）。"""
    prefix = f"{node.path}/"
    descendants = [row for row in nodes if row.path.startswith(prefix)]
    channels = [row for row in descendants if row.node_type == "channel"]
    metrics = [row for row in descendants if row.node_type == "metric"]
    attrs = dict(node.attrs)
    attrs["_channels"] = [row.name for row in channels]
    return ObjectRow(
        node_id=node.id,
        name=node.name,
        obj_type=str(node.attrs.get("类型", "")),
        part=str(node.attrs.get("部位", "")),
        device_name=device_name,
        channel_count=len(channels),
        metric_count=len(metrics),
        attrs=attrs,
    )


def list_objects(session: Session, task_id: int) -> list[ObjectRow]:
    """任务下全部监测对象（按树顺序）。"""
    nodes = tree.list_nodes(session, task_id)
    devices = {row.id: row.name for row in nodes if row.node_type == "device"}
    return [
        _build_row(row, nodes, devices.get(row.parent_id or 0, ""))
        for row in nodes
        if row.node_type == "component"
    ]


def get_object(session: Session, node_id: int) -> ObjectRow:
    node = tree.get_node(session, node_id)
    if node.node_type != "component":
        raise ObjectError(f"节点 {node_id} 不是监测对象（实际是 {node.type_text}）")
    nodes = tree.list_nodes(session, node.task_id)
    devices = {row.id: row.name for row in nodes if row.node_type == "device"}
    return _build_row(node, nodes, devices.get(node.parent_id or 0, ""))


def create_object(
    session: Session,
    task_id: int,
    device_id: int,
    name: str,
    obj_type: str,
    part: str = "",
) -> ObjectRow:
    """在设备下新增监测对象。"""
    kind = _check_type(obj_type)
    attrs: dict[str, Any] = {"类型": kind}
    if part.strip():
        attrs["部位"] = part.strip()
    node = tree.create_node(session, task_id, "component", name, parent_id=device_id, attrs=attrs)
    return get_object(session, node.id)


def update_object(
    session: Session,
    node_id: int,
    name: str | None = None,
    obj_type: str | None = None,
    part: str | None = None,
) -> ObjectRow:
    """改名称 / 对象类型 / 部位；未传的字段保持原值。"""
    current = tree.get_node(session, node_id)
    if current.node_type != "component":
        raise ObjectError(f"节点 {node_id} 不是监测对象（实际是 {current.type_text}）")

    attrs = dict(current.attrs)
    if obj_type is not None:
        attrs["类型"] = _check_type(obj_type)
    if part is not None:
        if part.strip():
            attrs["部位"] = part.strip()
        else:
            attrs.pop("部位", None)

    tree.update_node(session, node_id, name=name, attrs=attrs)
    return get_object(session, node_id)


def delete_object(session: Session, node_id: int) -> dict[str, int]:
    """级联软删该监测对象及其下通道、指标。"""
    current = tree.get_node(session, node_id)
    if current.node_type != "component":
        raise ObjectError(f"节点 {node_id} 不是监测对象（实际是 {current.type_text}）")
    return tree.soft_delete_node(session, node_id)
