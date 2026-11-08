"""健康树：节点 CRUD、层级校验、级联软删、嵌套快照、模板导入导出。

层级固定为 任务 → 设备 → 监测对象 → 通道 → 指标；阈值不进节点表，
由 thresholds 模块按「节点 × 指标」单存（见子模块 6）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import DeletionLog, MetricAssign, MetricLimit, Sensor, TaskNode

__all__ = [
    "CHILD_OF",
    "PARENT_OF",
    "RPM_ATTR",
    "path_nodes",
    "reparent_node",
    "root_node",
    "NODE_TYPE_TEXT",
    "NodeNotFoundError",
    "NodeRow",
    "TreeError",
    "create_node",
    "export_template",
    "get_node",
    "import_template",
    "list_nodes",
    "node_rpm",
    "node_path_text",
    "payload_version",
    "preview_import",
    "soft_delete_node",
    "sync_ref_name",
    "tree_snapshot",
    "update_node",
    "validate_template",
]

#: 每种节点类型要求的父节点类型（None = 根）
PARENT_OF: dict[str, str | None] = {
    "task": None,
    "device": "task",
    "component": "device",
    "channel": "component",
    "metric": "channel",
}

#: 每种节点类型可挂的子节点类型（界面"新增子节点"用）
CHILD_OF: dict[str, str] = {parent: child for child, parent in PARENT_OF.items() if parent}

#: 类型中文名（界面与导出的提示用）
NODE_TYPE_TEXT = {
    "task": "测试任务",
    "device": "设备",
    "component": "监测对象",
    "channel": "通道",
    "metric": "监测指标",
}

#: 模板文件标识
TEMPLATE_SCHEMA = "lion-monitor/health-tree"
#: 当前导出版本（v2：业务键引用 + 指标参数 + 阈值区段）
TEMPLATE_VERSION = 2
#: 仍可导入的历史版本（v1 用自增 id 引用，能读不推荐）
TEMPLATE_VERSIONS = (1, 2)


class TreeError(Exception):
    """健康树相关的业务错误（层级、名称等）。"""


class NodeNotFoundError(TreeError):
    """节点不存在或已被删除。"""


@dataclass(frozen=True, slots=True)
class NodeRow:
    """节点只读视图（脱开 ORM 生命周期）。"""

    id: int
    task_id: int
    parent_id: int | None
    node_type: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)
    path: str = ""
    ref_type: str = ""
    ref_id: int | None = None
    sort: int = 0
    deleted_at: datetime | None = None

    @property
    def type_text(self) -> str:
        return NODE_TYPE_TEXT.get(self.node_type, self.node_type)


def _load_attrs(model: TaskNode | None) -> dict[str, Any]:
    if model is None:
        return {}
    try:
        data = json.loads(model.attrs_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _to_row(model: TaskNode) -> NodeRow:
    return NodeRow(
        id=model.id,
        task_id=model.task_id,
        parent_id=model.parent_id,
        node_type=model.node_type,
        name=model.name,
        attrs=_load_attrs(model),
        path=model.path,
        ref_type=getattr(model, "ref_type", "") or "",
        ref_id=getattr(model, "ref_id", None),
        sort=model.sort,
        deleted_at=model.deleted_at,
    )


def _alive(model: TaskNode | None) -> bool:
    return model is not None and model.deleted_at is None


def _check_hierarchy(node_type: str, parent: TaskNode | None) -> None:
    if node_type not in PARENT_OF:
        allowed = " / ".join(PARENT_OF)
        raise TreeError(f"节点类型不合法：{node_type}（允许 {allowed}）")
    expected = PARENT_OF[node_type]
    if expected is None:
        if parent is not None:
            raise TreeError(f"{NODE_TYPE_TEXT[node_type]}是根节点，不能有父节点")
        return
    if parent is None:
        raise TreeError(f"{NODE_TYPE_TEXT[node_type]}的父节点必须是{expected}，不能为空")
    if parent.node_type != expected:
        actual = NODE_TYPE_TEXT.get(parent.node_type, parent.node_type)
        raise TreeError(
            f"层级不合法：{NODE_TYPE_TEXT[node_type]} 的父节点必须是 "
            f"{NODE_TYPE_TEXT[expected]}，实际是 {actual}"
        )


def create_node(
    session: Session,
    task_id: int,
    node_type: str,
    name: str,
    parent_id: int | None = None,
    attrs: dict[str, Any] | None = None,
    sort: int = 0,
    ref_type: str = "",
    ref_id: int | None = None,
) -> NodeRow:
    """建节点：校验层级与名称，落库后补 path（祖先 id 串）。"""
    clean = (name or "").strip()
    if not clean:
        raise TreeError("节点名称不能为空")

    parent = session.get(TaskNode, parent_id) if parent_id else None
    if parent_id and not _alive(parent):
        raise NodeNotFoundError(f"父节点不存在：{parent_id}")
    _check_hierarchy(node_type, parent)
    if parent is not None and parent.task_id != task_id:
        raise TreeError("父节点不属于该任务")

    ref_type, ref_id = _check_ref(session, task_id, node_type, ref_type, ref_id)
    _check_unique_ref(session, task_id, parent_id, ref_type, ref_id)

    model = TaskNode(
        task_id=task_id,
        parent_id=parent_id,
        node_type=node_type,
        name=clean,
        attrs_json=json.dumps(attrs or {}, ensure_ascii=False),
        ref_type=ref_type,
        ref_id=ref_id,
        sort=sort,
    )
    session.add(model)
    session.flush()
    model.path = f"{parent.path}/{model.id}" if parent is not None else str(model.id)
    if node_type == "channel":
        from .channel_reg import sync_ownership  # 延迟 import：避免循环依赖

        sync_ownership(session, task_id)
    session.commit()
    return _to_row(model)


#: 节点手填转速存在节点属性里（通道 / 监测对象都行，取最近的一级）
RPM_ATTR = "转速"


#: 只有监测对象在整棵树里唯一；通道（连同它的传感器）允许挂到多个监测对象下面，
#: 指标定义按通道各算各的 —— 余下的都只要求"同一个父节点下不重复"。
UNIQUE_IN_TASK = frozenset({"monitor_object"})


def _check_unique_ref(
    session: Session, task_id: int, parent_id: int | None, ref_type: str, ref_id: int | None
) -> None:
    """同一条台账记录不许重复装配。

    判定按 ``ref_type`` + ``ref_id``（不是名字——改名会绕过按名字的判断）：
    * 监测对象：整棵树只能出现一次；
    * 通道 / 指标定义：同一个父节点下只能出现一次（同一台传感器可挂多个监测对象）。
    """
    if not ref_type or ref_id is None:
        return
    stmt = select(TaskNode.id).where(
        TaskNode.task_id == task_id,
        TaskNode.ref_type == ref_type,
        TaskNode.ref_id == ref_id,
        TaskNode.deleted_at.is_(None),
    )
    if ref_type not in UNIQUE_IN_TASK:
        stmt = stmt.where(TaskNode.parent_id == parent_id)
    if session.execute(stmt).first() is not None:
        raise TreeError("这条台账记录已经装配在健康树上了（同一记录不能重复装配）")


def _rpm_value(raw: Any) -> Any:
    """节点属性里的转速取值：空 → None，能转数字就转（留痕里 1480 而不是 "1480"）。"""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def node_rpm(nodes: list[NodeRow], node_id: int, depth: int = 3) -> float | None:
    """节点手填转速：本节点 → 逐级往上（通道 → 监测对象 → 设备，最远 ``depth`` 级）。

    取第一个大于 0 的数值；拿不到返回 ``None``（**不猜**，无转速就不算频率类指标）。
    """
    by_id = {row.id: row for row in nodes}
    node = by_id.get(node_id)
    for _ in range(depth):
        if node is None:
            break
        raw = node.attrs.get(RPM_ATTR)
        try:
            value = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        node = by_id.get(node.parent_id) if node.parent_id else None
    return None


#: 节点类型 -> 应引用的台账类型
REF_TYPE_OF: dict[str, str] = {
    "component": "monitor_object",
    "channel": "channel_reg",
    "metric": "metric_def",
}


def _ledger_rows(session: Session, ref_type: str, task_id: int):
    """取台账未删行（延迟 import，避免环）。"""
    if ref_type == "monitor_object":
        from . import object_registry

        return {row.id for row in object_registry.list_objects(session, task_id)}
    if ref_type == "channel_reg":
        from . import channel_reg

        return {row.id for row in channel_reg.list_channels(session, task_id)}
    if ref_type == "metric_def":
        from . import metric_def

        return {row.id for row in metric_def.list_metrics(session, task_id)}
    return set()


def _check_ref(
    session: Session, task_id: int, node_type: str, ref_type: str, ref_id: int | None
) -> tuple[str, int | None]:
    """校验并归一化引用：节点类型决定应引用的台账类型。"""
    expected = REF_TYPE_OF.get(node_type, "")
    if ref_id is None:
        if ref_type:
            raise TreeError(f"节点类型 {node_type} 没有对应台账，不能设置引用")
        return "", None
    if ref_type and expected and ref_type != expected:
        raise TreeError(f"台账类型不符：{node_type} 节点应引用 {expected}，收到 {ref_type}")
    if not expected:
        raise TreeError(f"节点类型 {node_type} 没有对应台账，不能设置引用")
    if ref_id not in _ledger_rows(session, expected, task_id):
        raise TreeError(f"台账中找不到 {expected} 记录：{ref_id}")
    return expected, ref_id


def list_nodes(session: Session, task_id: int, include_deleted: bool = False) -> list[NodeRow]:
    """任务下的节点，按 path 的数字段排序（父在子前）；默认不含已删。"""
    stmt = select(TaskNode).where(TaskNode.task_id == task_id).order_by(TaskNode.id)
    if not include_deleted:
        stmt = stmt.where(TaskNode.deleted_at.is_(None))
    rows = [_to_row(model) for model in session.execute(stmt).scalars()]
    rows.sort(key=lambda row: tuple(int(part) for part in row.path.split("/") if part))
    return rows


def path_nodes(session: Session, node_id: int) -> list[NodeRow]:
    """从根到该节点的链路（含自身），用于阈值继承。"""
    node = get_node(session, node_id)
    nodes = {row.id: row for row in list_nodes(session, node.task_id)}
    chain = [nodes[int(part)] for part in node.path.split("/") if int(part) in nodes]
    return chain or [node]


def root_node(session: Session, task_id: int) -> NodeRow:
    """任务的根节点（node_type=task）；没有则报错。"""
    stmt = select(TaskNode).where(
        TaskNode.task_id == task_id,
        TaskNode.node_type == "task",
        TaskNode.deleted_at.is_(None),
    )
    model = session.execute(stmt).scalars().first()
    if model is None:
        raise NodeNotFoundError(f"任务 {task_id} 还没有根节点")
    return _to_row(model)


def get_node(session: Session, node_id: int) -> NodeRow:
    """取节点；已删除视为不存在。"""
    model = session.get(TaskNode, node_id)
    if not _alive(model):
        raise NodeNotFoundError(f"节点不存在：{node_id}")
    return _to_row(model)


def update_node(
    session: Session,
    node_id: int,
    name: str | None = None,
    attrs: dict[str, Any] | None = None,
    sort: int | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    clear_ref: bool = False,
) -> NodeRow:
    """改节点：只允许 name / attrs / sort / 引用，类型与父子关系不动（改层级请删后重建）。

    ``clear_ref=True`` 会清空台账引用（台账行已被删的"悬空引用"用它转成手工节点）。
    """
    model = session.get(TaskNode, node_id)
    if not _alive(model):
        raise NodeNotFoundError(f"节点不存在：{node_id}")
    if clear_ref:
        model.ref_type, model.ref_id = "", None
    elif ref_id is not None:
        model.ref_type, model.ref_id = _check_ref(
            session, model.task_id, model.node_type, ref_type or "", ref_id
        )

    if name is not None:
        clean = name.strip()
        if not clean:
            raise TreeError("节点名称不能为空")
        model.name = clean
    if attrs is not None:
        before_rpm = _rpm_value(_load_attrs(model).get(RPM_ATTR))
        model.attrs_json = json.dumps(attrs, ensure_ascii=False)
        after_rpm = _rpm_value(attrs.get(RPM_ATTR))
        if not audit.same_value(before_rpm, after_rpm):
            # T3：转速是频率类指标的基准，改了要留痕（谁、什么时候、从多少改到多少）
            audit.record_change(
                session,
                entity_type="tree_node",
                entity_id=model.id,
                field=RPM_ATTR,
                old_value=before_rpm,
                new_value=after_rpm,
                actor=audit.actor_of(session, model.task_id),
                note=model.name,
            )
    if sort is not None:
        model.sort = sort

    from .channel_reg import sync_ownership  # 延迟 import：避免循环依赖

    sync_ownership(session, model.task_id)
    session.commit()
    return _to_row(model)


def reparent_node(session: Session, node_id: int, parent_id: int) -> NodeRow:
    """把节点挪到另一个父节点下（同任务、层级合法），并刷新整棵子树的 ``path``。

    用于「传感器改绑」：通道节点从旧对象挪到新对象下，指标授权 / 阈值跟着节点走。
    """
    model = session.get(TaskNode, node_id)
    if not _alive(model):
        raise NodeNotFoundError(f"节点不存在：{node_id}")
    parent = session.get(TaskNode, parent_id)
    if parent is None or parent.deleted_at is not None:
        raise NodeNotFoundError(f"父节点不存在：{parent_id}")
    if parent.task_id != model.task_id:
        raise TreeError("不能跨任务移动节点")
    _check_hierarchy(model.node_type, parent)

    old = model.path or ""
    model.parent_id = parent.id
    model.path = f"{parent.path}/{model.id}"
    session.flush()
    if old and old != model.path:
        children = session.execute(select(TaskNode).where(TaskNode.path.like(f"{old}/%"))).scalars()
        for child in list(children):
            child.path = model.path + child.path[len(old) :]
    from .channel_reg import sync_ownership  # 延迟 import：避免循环依赖

    sync_ownership(session, model.task_id)
    session.commit()
    return _to_row(model)


def sync_ref_name(session: Session, ref_type: str, ref_id: int | None, name: str) -> int:
    """台账改名后同步引用该台账的节点显示名（只动未删节点），返回改动条数。

    不改台账、不提交事务：由调用方（台账 update_*）负责 commit。
    """
    clean = (name or "").strip()
    if not clean or not ref_type or ref_id is None:
        return 0
    stmt = select(TaskNode).where(
        TaskNode.ref_type == ref_type,
        TaskNode.ref_id == ref_id,
        TaskNode.deleted_at.is_(None),
    )
    changed = 0
    for model in session.execute(stmt).scalars():
        if model.name != clean:
            model.name = clean
            changed += 1
    return changed


def _subtree_models(session: Session, node_id: int) -> list[TaskNode]:
    """含自己的整棵子树（只取未删的）。"""

    root = session.get(TaskNode, node_id)
    if not _alive(root):
        raise NodeNotFoundError(f"节点不存在：{node_id}")

    nodes = {
        model.id: model
        for model in session.execute(
            select(TaskNode)
            .where(TaskNode.task_id == root.task_id, TaskNode.deleted_at.is_(None))
            .order_by(TaskNode.path)
        ).scalars()
    }
    picked = [root]
    frontier = {root.id}
    while frontier:
        children = [model for model in nodes.values() if model.parent_id in frontier]
        picked.extend(children)
        frontier = {model.id for model in children}
    return picked


def _assign_snapshot(model: MetricAssign) -> dict[str, Any]:
    return {
        "id": model.id,
        "task_id": model.task_id,
        "node_id": model.node_id,
        "metric_code": model.metric_code,
        "unit": model.unit,
        "params_json": model.params_json,
        "params_locked": int(model.params_locked or 0),
        "enabled": int(model.enabled or 0),
    }


def _limit_snapshot(model: MetricLimit) -> dict[str, Any]:
    return {
        "id": model.id,
        "task_id": model.task_id,
        "node_id": model.node_id,
        "metric_code": model.metric_code,
        "warn": float(model.warn),
        "alert": float(model.alert),
        "direction": model.direction,
        "enabled": int(model.enabled or 0),
    }


def _sensor_snapshot(model: Sensor) -> dict[str, Any]:
    """通道节点上传感器的快照（回收站恢复时按它复原）。"""
    return {
        "id": model.id,
        "task_id": model.task_id,
        "node_id": model.node_id,
        "channel_code": model.channel_code,
        "model": model.model,
        "sensitivity": model.sensitivity,
        "range_g": model.range_g,
        "mounting": model.mounting,
        "calibrated_on": model.calibrated_on,
        "status": model.status,
        "remark": model.remark,
    }


def node_path_text(session: Session, model: TaskNode) -> str:
    """节点在树里的可读路径（「任务 / 设备 / 监测对象 / 通道」），回收站列表用。"""
    names: list[str] = []
    for part in (model.path or "").split("/"):
        if not part:
            continue
        ancestor = session.get(TaskNode, int(part))
        if ancestor is not None:
            names.append(ancestor.name)
    return " / ".join(names) or model.name


def _snapshot_node(model: TaskNode, attrs: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model.id,
        "task_id": model.task_id,
        "parent_id": model.parent_id,
        "node_type": model.node_type,
        "name": model.name,
        "attrs": attrs,
        "ref_type": model.ref_type or "",
        "ref_id": model.ref_id,
        "sort": model.sort,
        "path": model.path,
        "children": [],
    }


def _recycle_payload(
    session: Session,
    models: list[TaskNode],
    assigns: list[MetricAssign],
    limits: list[MetricLimit],
    *,
    nested: bool,
    sensors: list[Sensor] | None = None,
) -> dict[str, Any]:
    """子树快照 JSON：根节点（含 ``children``）+ 这些节点上的指标授权 / 阈值 / 传感器。"""
    by_id = {model.id: _snapshot_node(model, _load_attrs(model)) for model in models}
    root = by_id[models[0].id]
    if nested:
        for model in models[1:]:
            parent = by_id.get(model.parent_id) if model.parent_id is not None else None
            if parent is not None:
                parent["children"].append(by_id[model.id])
    return {
        "node": root,
        "assigns": [_assign_snapshot(row) for row in assigns],
        "limits": [_limit_snapshot(row) for row in limits],
        "sensors": [_sensor_snapshot(row) for row in (sensors or [])],
    }


def soft_delete_node(
    session: Session,
    node_id: int,
    operator: str = "mark",
    log_each_node: bool = False,
    cascade_sensors: bool = True,
) -> dict[str, int]:
    """软删节点及其子树，返回各类节点的删除条数（含自己）。

    G6：同时写 ``deletion_log`` 留痕——``payload`` 是子树快照（节点属性 + 子节点 + 该子树
    上的指标授权 / 阈值），``expires_at`` = 现在 + 保留期（默认 7 天）。子树上的授权与阈值
    一并软删，恢复时再一起复原（否则回收站恢复回来是个「没有授权的空壳」）。

    ``log_each_node=True``：子树里每个节点各写一条留痕（模板整树替换用，条数 = 被替换节点数）。
    """
    from .recycle import expiry_of  # 延迟 import：recycle 反过来依赖本模块

    models = _subtree_models(session, node_id)
    now = datetime.now()
    expires_at = expiry_of(now)
    counts: dict[str, int] = {}
    for model in models:
        model.deleted_at = now
        counts[model.node_type] = counts.get(model.node_type, 0) + 1

    ids = [model.id for model in models]
    assigns = list(
        session.execute(
            select(MetricAssign).where(
                MetricAssign.node_id.in_(ids), MetricAssign.deleted_at.is_(None)
            )
        ).scalars()
    )
    limits = list(
        session.execute(
            select(MetricLimit).where(
                MetricLimit.node_id.in_(ids), MetricLimit.deleted_at.is_(None)
            )
        ).scalars()
    )
    for row in assigns:
        row.deleted_at = now
    for row in limits:
        row.deleted_at = now

    # 传感器是台账资产，删掉「装配」不等于报废传感器：
    # 节点进回收站时把传感器解绑（node_id=None）留在台账当自由态，恢复节点时再挂回去。
    # 马哥 2026-11-02：「把监测对象下的传感器节点删除了，传感器应该回到自由状态」。
    sensors_rows = list(
        session.execute(
            select(Sensor).where(Sensor.node_id.in_(ids), Sensor.deleted_at.is_(None))
        ).scalars()
    )
    payload_sensors = sensors_rows if cascade_sensors else []
    for row in sensors_rows:
        row.node_id = None
    channel_ids = {
        model.ref_id for model in models if model.node_type == "channel" and model.ref_id
    }
    if channel_ids:
        from .channel_reg import release_unassembled  # 延迟 import：避免与 channel_reg 循环

        session.flush()
        release_unassembled(session, models[0].task_id)

    if log_each_node:
        for model in models:
            payload = _recycle_payload(
                session,
                [model],
                [row for row in assigns if row.node_id == model.id],
                [row for row in limits if row.node_id == model.id],
                nested=False,
                sensors=[row for row in payload_sensors if row.node_id == model.id],
            )
            session.add(
                _deletion_log(
                    model,
                    node_path_text(session, model),
                    {model.node_type: 1},
                    operator,
                    payload,
                    now,
                    expires_at,
                )
            )
    else:
        session.add(
            _deletion_log(
                models[0],
                node_path_text(session, models[0]),
                counts,
                operator,
                _recycle_payload(
                    session, models, assigns, limits, nested=True, sensors=payload_sensors
                ),
                now,
                expires_at,
            )
        )
    session.commit()
    return counts


def _deletion_log(
    model: TaskNode,
    name: str,
    counts: dict[str, int],
    operator: str,
    payload: dict[str, Any],
    deleted_at: datetime,
    expires_at: datetime,
) -> DeletionLog:
    return DeletionLog(
        entity_type="task_node",
        entity_id=model.id,
        entity_name=name,
        mode="soft",
        cascade_counts=json.dumps(counts, ensure_ascii=False),
        operator=operator,
        payload=json.dumps(payload, ensure_ascii=False),
        expires_at=expires_at,
        created_at=deleted_at,
    )


def _node_dict(row: NodeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.node_type,
        "name": row.name,
        "attrs": dict(row.attrs),
        "ref_type": row.ref_type,
        "ref_id": row.ref_id,
        "children": [],
    }


def tree_snapshot(session: Session, task_id: int) -> dict[str, Any] | None:
    """把任务下的节点拼成嵌套结构（给界面渲染）；空树返回 None。"""
    rows = list_nodes(session, task_id)
    if not rows:
        return None

    index = {row.id: _node_dict(row) for row in rows}
    roots: list[dict[str, Any]] = []
    for row in rows:
        if row.parent_id is None or row.parent_id not in index:
            roots.append(index[row.id])
        else:
            index[row.parent_id]["children"].append(index[row.id])
    return roots[0] if roots else None


def export_template(
    session: Session, task_id: int, include_thresholds: bool = True
) -> dict[str, Any]:
    """导出整棵树为 v2 模板 JSON（业务键引用 + 指标参数 + 可选阈值区段）。"""
    from . import tree_template  # 延迟 import：tree_template 反过来依赖本模块

    return tree_template.export_template(session, task_id, include_thresholds=include_thresholds)


def _validate_template_ref(node: dict[str, Any], node_type: str) -> None:
    """模板节点引用校验（不查库）：ref_type/ref_id 必须成对，且与节点类型对应台账一致。"""
    ref_type = node.get("ref_type") or ""
    ref_id = node.get("ref_id")
    if not ref_type and ref_id is None:
        return
    if not ref_type:
        raise TreeError("引用缺少 ref_type：有 ref_id 时必须写明台账类型")
    if ref_id is None:
        raise TreeError(f"引用缺少 ref_id：{ref_type} 必须给出引用行 id")
    if isinstance(ref_id, bool) or not isinstance(ref_id, int) or ref_id <= 0:
        raise TreeError(f"引用 ref_id 必须是正整数：{ref_id!r}")
    expected = REF_TYPE_OF.get(node_type, "")
    if not expected:
        raise TreeError(f"节点类型 {node_type} 没有对应台账，不能设置引用")
    if ref_type != expected:
        raise TreeError(
            f"台账类型不符：{NODE_TYPE_TEXT[node_type]}节点应引用 {expected}，模板里是 {ref_type}"
        )


def _validate_template_node(node: dict[str, Any], parent_type: str | None) -> None:
    if not isinstance(node, dict):
        raise TreeError("模板节点必须是对象")
    node_type = node.get("type")
    if node_type not in PARENT_OF:
        allowed = " / ".join(PARENT_OF)
        raise TreeError(f"节点类型不合法：{node_type}（允许 {allowed}）")
    expected = PARENT_OF[node_type]
    if expected != parent_type:
        want = NODE_TYPE_TEXT[expected] if expected else "无（只能作为根）"
        raise TreeError(f"层级不合法：{NODE_TYPE_TEXT[node_type]}的父节点应为 {want}")
    if not (node.get("name") or "").strip():
        raise TreeError("名称不能为空：模板里存在空名称节点")
    attrs = node.get("attrs")
    if attrs is not None and not isinstance(attrs, dict):
        raise TreeError("attrs 必须是对象")
    _validate_template_ref(node, node_type)
    for child in node.get("children") or []:
        _validate_template_node(child, node_type)


def _check_template_refs(session: Session, task_id: int, node: dict[str, Any]) -> None:
    """落库前查台账：整棵树里每个引用都要在当前任务的台账里存在。"""
    _check_ref(
        session,
        task_id,
        node["type"],
        node.get("ref_type") or "",
        node.get("ref_id"),
    )
    for child in node.get("children") or []:
        _check_template_refs(session, task_id, child)


def payload_version(payload: dict[str, Any]) -> int:
    """模板外壳校验并返回版本号（1 / 2）；未知版本明确报错。"""
    if not isinstance(payload, dict):
        raise TreeError("模板内容必须是 JSON 对象")
    if payload.get("schema") != TEMPLATE_SCHEMA:
        raise TreeError(f"模板标识不匹配：需要 {TEMPLATE_SCHEMA}")
    version = payload.get("version")
    if version not in TEMPLATE_VERSIONS:
        allowed = " / ".join(str(item) for item in TEMPLATE_VERSIONS)
        raise TreeError(f"模板版本不支持：{version}（支持 {allowed}）")
    return 2 if version == 2 else 1


def _check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """模板外壳与结构校验（v1），返回 root 节点（不落库）。"""
    payload_version(payload)
    root = payload.get("root")
    if not isinstance(root, dict):
        raise TreeError("模板缺少 root 节点")
    _validate_template_node(root, None)
    return root


def _count_template(node: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        counts[current["type"]] = counts.get(current["type"], 0) + 1
        stack.extend(current.get("children") or [])
    return counts


def validate_template(payload: dict[str, Any]) -> dict[str, int]:
    """只校验模板，返回各类节点条数预览；不碰数据库（v1 / v2 都支持）。"""
    if payload_version(payload) == 2:
        from . import tree_template

        return tree_template.validate_template(payload)
    return _count_template(_check_payload(payload))


def preview_import(session: Session, task_id: int, payload: dict[str, Any]):
    """导入前预览（v2）：节点/指标/阈值条数 + 未匹配台账清单；不写库。"""
    from . import tree_template

    return tree_template.preview_import(session, task_id, payload)


def import_template(
    session: Session,
    task_id: int,
    payload: dict[str, Any],
    options=None,
):
    """导入模板（v2 支持范围与策略；v1 走旧的自增 id 逻辑）。

    返回 ``ImportReport``（``node_counts`` 是各类节点条数，兼容旧的 counts["device"] 用法）。
    """
    from . import tree_template

    if payload_version(payload) == 2:
        return tree_template.import_template(session, task_id, payload, options)
    return _import_template_v1(session, task_id, payload)


def _import_template_v1(session: Session, task_id: int, payload: dict[str, Any]):
    """v1 模板导入：整棵树替换（引用用旧的自增 ref_id）。"""
    from . import tree_template

    root = _check_payload(payload)
    _check_template_refs(session, task_id, root)

    for row in list_nodes(session, task_id):
        if row.parent_id is None:
            soft_delete_node(session, row.id, log_each_node=True)

    counts: dict[str, int] = {}

    def _create(node: dict[str, Any], parent_id: int | None) -> None:
        created = create_node(
            session,
            task_id,
            node["type"],
            node["name"],
            parent_id=parent_id,
            attrs=dict(node.get("attrs") or {}),
            ref_type=node.get("ref_type") or "",
            ref_id=node.get("ref_id"),
        )
        counts[created.node_type] = counts.get(created.node_type, 0) + 1
        for child in node.get("children") or []:
            _create(child, created.id)

    _create(root, None)
    return tree_template.ImportReport(
        version=1, node_counts=counts, notice=tree_template.LEGACY_NOTE
    )
