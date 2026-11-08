"""工作台概览：任务数、数据集数、超限记录、健康分布、设备总览、九步进度。

所有数字来自数据库（test_task / task_node / dataset / channel / metric_value / metric_limit），
口径与趋势分析页保持一致：

- **超限记录**：每个「通道节点 × 指标」的每个越限计算点算一条（逐点，不聚合）；
  同一通道号挂多个监测对象时各算各的（P2 / S1），不再按通道号去重。
- **健康分布**：指标节点按最新一点判级（ok / warn / alert / unset），见 ``features.health``。
- **九步进度**：每一步由库里是否有对应数据决定，第一个未完成的步骤标 current。

P3 / S2：超限记录只从库里取越限那几行（阈值条件下推到 SQL），健康分布 / 设备总览复用
一次 ``node_health`` 的结果，九步的 ⑨ 用「每条序列的行数」判断 —— 都不再把整串指标值
拉回 Python 遍历。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..store import datasets as dataset_store
from ..store import limits, metric_def, object_registry, sensors, tasks, tree
from ..store import metrics as metric_store
from .health import node_health, point_level
from .series import MetricScope, metric_scopes

#: 健康度窗口：取对象最新的 N 个有效指标点（2026-11-04 定版）
HEALTH_WINDOW = 5

#: 健康度：值 / 阈值的均值，1.0 = 踩到严重线
HealthScore = tuple[float | None, int]


#: 九步流程（编号, 名称）
STEPS: tuple[tuple[str, str], ...] = (
    ("①", "测试任务"),
    ("②", "监测对象"),
    ("③", "传感器"),
    ("④", "指标配置"),
    ("⑤", "数据接入"),
    ("⑥", "有效性"),
    ("⑦", "指标计算"),
    ("⑧", "阈值"),
    ("⑨", "趋势"),
)

#: 健康状态（顺序即展示顺序）；``invalid`` = 值非有限（NaN/Inf），无法判级
HEALTH_STATES: tuple[str, ...] = ("ok", "warn", "alert", "invalid", "unset")

#: 越限两级
OVER_LEVELS: tuple[str, ...] = ("warn", "alert")


@dataclass(slots=True)
class OverRecord:
    """一条越限记录：通道节点 × 指标 × 一个计算点。"""

    channel: str
    code: str
    computed_at: str
    value: float
    unit: str
    level: str
    warn: float
    alert: float
    source: str
    node_id: int | None = None  #: 值行归属的通道节点 id（老数据为空）
    #: 该通道节点所属的监测对象名（待办要显示「监测对象」）
    object_name: str = ""


@dataclass(slots=True)
class OverTodo:
    """待办里的一条「幅值超限」：一个（监测对象, 通道, 指标）组合。"""

    object_name: str
    channel: str
    code: str
    #: 最新那条越限记录的等级（alert / warn）
    level: str
    #: 最新那条越限记录的时间
    computed_at: str
    #: 该组合累计越限点数
    points: int


@dataclass(slots=True)
class DeviceOverview:
    """设备级总览：状态取子树最差，计数取子树内节点。"""

    node_id: int
    name: str
    state: str
    object_count: int
    channel_count: int
    metric_count: int
    over_metric_count: int


@dataclass(slots=True)
class ChannelView:
    """监测对象名下一个测点（通道节点）的状态。"""

    node_id: int
    name: str
    state: str = "unset"
    over_count: int = 0


@dataclass(slots=True)
class ObjectView:
    """监测对象卡片：设备归属 + 自身状态 + 名下测点 + 指标读数。

    工作台「设备健康状态」按这个结构画数字孪生卡片（2026-11-04）：
    一眼看到「哪台设备 → 哪个对象 → 哪几个测点 → 什么状态」。
    """

    node_id: int
    name: str
    type_label: str
    state: str
    device_id: int
    device_name: str
    channels: list[ChannelView] = field(default_factory=list)
    metric_count: int = 0
    over_metric_count: int = 0
    stale_count: int = 0
    #: 最严重那个指标的最新读数（"v_RMS 2.31 mm/s"），没有有效点时为 ""
    worst_reading: str = ""
    #: 健康度 = 最新 5 个指标点 mean(值/阈值)；1.0 = 严重线。无可用点 → None
    health_score: float | None = None
    #: 参与健康度计算的点数
    health_points: int = 0

    @property
    def channel_count(self) -> int:
        return len(self.channels)


@dataclass(slots=True)
class TaskOverview:
    """工作台一次渲染需要的全部数字。"""

    task_id: int | None
    task_count: int
    dataset_count: int
    channel_count: int
    metric_value_count: int
    over_records: list[OverRecord] = field(default_factory=list)
    health_counts: dict[str, int] = field(default_factory=dict)
    devices: list[DeviceOverview] = field(default_factory=list)
    objects: list[ObjectView] = field(default_factory=list)
    steps: list[tuple[str, str, str]] = field(default_factory=list)
    pending_validity: int = 0
    #: 由旧参数算出、还没重算的指标值行数（S4A：工作台直接显示「待重算 N 条」）
    stale_count: int = 0

    @property
    def over_count(self) -> int:
        return len(self.over_records)

    @property
    def over_todos(self) -> list[OverTodo]:
        """待办用的幅值超限：按（监测对象, 通道, 指标）聚合。

        ``over_records`` 按计算时间升序，所以同一组合最后一条就是最新一条：
        等级与时间取最新，点数取条数。
        """
        grouped: dict[tuple[str, str, str], OverTodo] = {}
        for row in self.over_records:
            key = (row.object_name, row.channel, row.code)
            current = grouped.get(key)
            if current is None:
                grouped[key] = OverTodo(
                    object_name=row.object_name,
                    channel=row.channel,
                    code=row.code,
                    level=row.level,
                    computed_at=row.computed_at,
                    points=1,
                )
            else:
                current.points += 1
                current.level = row.level
                current.computed_at = row.computed_at
        return list(grouped.values())

    def over_level_count(self, level: str) -> int:
        return sum(1 for row in self.over_records if row.level == level)


def _metric_scopes(
    session: Session, task_id: int, nodes: list[tree.NodeRow] | None = None
) -> list[MetricScope]:
    """指标节点 × 取数范围（一次读树，不查祖先链、不拉值）。"""
    return metric_scopes(session, task_id, nodes)


# ---------------------------------------------------------------- 超限记录


def over_records(session: Session, task_id: int) -> list[OverRecord]:
    """逐点列出越过 warn 的记录（按计算时间升序）；没设阈值的指标不算。

    键是「通道节点 × 指标 × 计算点」：同一通道号挂两个监测对象时各算各的（P2 / S1），
    同一通道节点下多个指标节点共读一份值时只算一次。
    """
    nodes = tree.list_nodes(session, task_id)
    scopes = _metric_scopes(session, task_id, nodes)
    if not scopes:
        return []

    codes = {item.metric_node.id: item.code for item in scopes}
    resolved = limits.resolve_limits(session, task_id, codes, nodes=nodes)

    # 同一阈值合成一条 SQL（阈值条件下推到库里，只取越限行）
    groups: dict[tuple[float, float, str, str], list[MetricScope]] = {}
    for item in scopes:
        limit = resolved.get(item.metric_node.id)
        if limit is not None:
            groups.setdefault(limit, []).append(item)

    channel_names = {item.channel_node.id: item.channel_node.name for item in scopes}
    by_id = {node.id: node for node in nodes}

    def object_of(node_id: int | None) -> str:
        """通道节点往上找监测对象（component）名。"""
        node = by_id.get(node_id) if node_id is not None else None
        while node is not None:
            if node.node_type == "component":
                return node.name
            node = by_id.get(node.parent_id) if node.parent_id else None
        return ""

    sole_channel = {item.channel_node.name: item.channel_node.id for item in scopes}

    found: list[OverRecord] = []
    seen: set[tuple[int | None, str, str]] = set()
    for limit, group in groups.items():
        warn, alert, _direction, source = limit
        rows = metric_store.over_rows_for_scopes(
            session, task_id, [item.series_scope() for item in group], limit
        )
        for row in rows:
            level = point_level(row.value, limit)
            if level not in OVER_LEVELS:  # SQL 已挡非有限值，这里双保险
                continue
            node_id = row.node_id if row.node_id is not None else sole_channel.get(row.channel_code)
            key = (node_id, row.metric_code, row.computed_at)
            if key in seen:
                continue
            seen.add(key)
            channel_name = channel_names.get(node_id, "") if node_id is not None else ""
            found.append(
                OverRecord(
                    channel=channel_name or row.channel_code,
                    code=row.metric_code,
                    computed_at=row.computed_at.strftime("%Y-%m-%d %H:%M:%S"),
                    value=float(row.value),
                    unit=row.unit,
                    level=level,
                    warn=float(warn),
                    alert=float(alert),
                    source=source,
                    node_id=node_id,
                    object_name=object_of(node_id),
                )
            )
    found.sort(key=lambda row: (row.computed_at, row.channel, row.code))
    return found


# ---------------------------------------------------------------- 健康分布


def health_counts(session: Session, task_id: int, health: dict | None = None) -> dict[str, int]:
    """指标节点的状态分布（ok / warn / alert / unset）。"""
    rows = node_health(session, task_id) if health is None else health
    counts = {state: 0 for state in HEALTH_STATES}
    for node in tree.list_nodes(session, task_id):
        if node.node_type != "metric":
            continue
        item = rows.get(node.id)
        state = item.state if item is not None else "unset"
        counts[state] = counts.get(state, 0) + 1
    return counts


# ---------------------------------------------------------------- 设备总览


def device_overviews(
    session: Session, task_id: int, health: dict | None = None
) -> list[DeviceOverview]:
    """每台设备一行：状态 + 子树内对象/通道/指标计数 + 超限指标数。"""
    nodes = tree.list_nodes(session, task_id)
    rows = node_health(session, task_id) if health is None else health
    found: list[DeviceOverview] = []
    for device in nodes:
        if device.node_type != "device":
            continue
        prefix = f"{device.path}/"
        subtree = [
            item for item in nodes if item.path == device.path or item.path.startswith(prefix)
        ]
        metrics_in = [item for item in subtree if item.node_type == "metric"]
        over = sum(
            1
            for item in metrics_in
            if rows.get(item.id) is not None and rows[item.id].state in OVER_LEVELS
        )
        own = rows.get(device.id)
        found.append(
            DeviceOverview(
                node_id=device.id,
                name=device.name,
                state=own.state if own is not None else "unset",
                object_count=sum(1 for item in subtree if item.node_type == "component"),
                channel_count=sum(1 for item in subtree if item.node_type == "channel"),
                metric_count=len(metrics_in),
                over_metric_count=over,
            )
        )
    return found


#: 严重程度排序：卡片挑「最严重那个指标」展示读数
_STATE_RANK = {"alert": 4, "warn": 3, "invalid": 2, "unset": 1, "ok": 0}


def object_health_scores(
    session: Session,
    task_id: int,
    nodes: list | None = None,
    scopes: list[MetricScope] | None = None,
) -> dict[int, HealthScore]:
    """每个监测对象 → (健康度, 参与点数)。

    口径（2026-11-04）：**该对象最新的 5 个指标点的 mean(指标/阈值)**。

    - 点来自对象子树内所有指标节点，按 ``computed_at`` 倒序取最新 ``HEALTH_WINDOW`` 个有效点；
    - 阈值优先 ``alert``（严重线），没设 alert 才用 ``warn``，两者都为 0 视为未设阈值 → 该点跳过；
    - ``stale`` 点不参与（旧参数算出来的值）；
    - 方向为「越低越差」（``<``）的指标反算 ``阈值 / 值``，保持「越大越差」；
    - 结果 **clamp 到 [0, 1]**（2026-11-04：最大不超过 1、最小不低于 0）；
    - 一个可算的点都没有 → ``(None, 0)``，界面显示「—」，不许当 0。
    """
    nodes = nodes if nodes is not None else tree.list_nodes(session, task_id)
    scopes = scopes if scopes is not None else metric_scopes(session, task_id, nodes)
    if not scopes:
        return {}
    by_id = {node.id: node for node in nodes}
    codes = {item.metric_node.id: item.code for item in scopes}
    resolved = limits.resolve_limits(session, task_id, codes, nodes=nodes)
    window = metric_store.recent_values_batch(
        session, task_id, [item.series_scope() for item in scopes], n=HEALTH_WINDOW
    )

    attached: dict[int, list[tuple[str, float]]] = {}
    for item in scopes:
        limit = resolved.get(item.metric_node.id)
        if limit is None:
            continue
        warn, alert, direction, _ = limit
        threshold = alert if alert else warn
        if not threshold:
            continue
        object_id = _component_of(by_id, item.metric_node)
        if object_id is None:
            continue
        for row in window.get(item.series_scope(), []):
            if row.stale:
                continue
            value = float(row.value)
            if not math.isfinite(value):
                continue
            if direction == "<":
                if value <= 0:
                    continue
                ratio = threshold / value
            else:
                ratio = value / threshold
            attached.setdefault(object_id, []).append((str(row.computed_at), ratio))

    found: dict[int, HealthScore] = {
        node.id: (None, 0) for node in nodes if node.node_type == "component"
    }
    for object_id, rows in attached.items():
        rows.sort(key=lambda pair: pair[0], reverse=True)
        picked = [ratio for _stamp, ratio in rows[:HEALTH_WINDOW]]
        avg = sum(picked) / len(picked)
        # 2026-11-04：健康度最大不超过 1、最小不低于 0（比值超过阈值一律记 1.00）
        found[object_id] = (min(max(avg, 0.0), 1.0), len(picked))
    return found


def _component_of(by_id: dict, node) -> int | None:
    """指标节点往上找到所属监测对象节点（component）。"""
    current = node
    while current is not None:
        if current.node_type == "component":
            return current.id
        current = by_id.get(current.parent_id) if current.parent_id else None
    return None


def object_overviews(
    session: Session,
    task_id: int,
    health: dict | None = None,
    scores: dict[int, HealthScore] | None = None,
) -> list[ObjectView]:
    """每个监测对象一张卡：设备归属、状态、名下测点状态、指标与超限计数。

    状态不重算：直接复用 ``node_health`` 的结果（工作台整页只算一次）。
    测点按 parent 归属，同一通道挂多个对象时各对象各显示一份（通道按父节点判重）。
    """
    nodes = tree.list_nodes(session, task_id)
    rows = node_health(session, task_id) if health is None else health
    found_scores = object_health_scores(session, task_id, nodes=nodes) if scores is None else scores
    devices = {item.id: item for item in nodes if item.node_type == "device"}
    # 对象类型在台账里（monitor_object.obj_type），节点属性只兜底
    registry = {row.id: row for row in object_registry.list_objects(session, task_id)}
    children: dict[int | None, list] = {}
    for item in nodes:
        children.setdefault(item.parent_id, []).append(item)

    def walk(node) -> list:
        found = [node]
        for child in children.get(node.id, []):
            found.extend(walk(child))
        return found

    found: list[ObjectView] = []
    for node in nodes:
        if node.node_type != "component":
            continue
        subtree = walk(node)
        metric_nodes = [item for item in subtree if item.node_type == "metric"]
        ranked = sorted(
            (
                item
                for item in metric_nodes
                if rows.get(item.id) is not None and rows[item.id].latest_value is not None
            ),
            key=lambda item: _STATE_RANK.get(rows[item.id].state, 0),
            reverse=True,
        )
        worst_reading = ""
        if ranked:
            top = rows[ranked[0].id]
            worst_reading = f"{ranked[0].name} {top.latest_value:.4g} {top.unit}".strip()
        own = rows.get(node.id)
        # 对象状态取子树最差（与 node_health 的向上汇总同口径）；
        # 不直接信 own —— 传入的 health 可能是局部构造的，子树状态才是用户看到的
        subtree_states = [rows[item.id].state for item in subtree if rows.get(item.id) is not None]
        object_state = max(subtree_states or ["unset"], key=lambda state: _STATE_RANK.get(state, 0))
        device = devices.get(_device_of(nodes, node))
        found.append(
            ObjectView(
                node_id=node.id,
                name=node.name,
                type_label=str(
                    node.attrs.get("object_type")
                    or (
                        registry[node.ref_id].obj_type
                        if node.ref_id in registry and node.ref_type == "monitor_object"
                        else ""
                    )
                    or ""
                ),
                state=object_state,
                device_id=device.id if device is not None else 0,
                device_name=device.name if device is not None else "",
                channels=[
                    ChannelView(
                        node_id=item.id,
                        name=item.name,
                        state=(rows[item.id].state if rows.get(item.id) is not None else "unset"),
                        over_count=(
                            rows[item.id].over_count if rows.get(item.id) is not None else 0
                        ),
                    )
                    for item in children.get(node.id, [])
                    if item.node_type == "channel"
                ],
                metric_count=len(metric_nodes),
                over_metric_count=sum(
                    1
                    for item in metric_nodes
                    if rows.get(item.id) is not None and rows[item.id].state in OVER_LEVELS
                ),
                stale_count=own.stale_count if own is not None else 0,
                worst_reading=worst_reading,
                health_score=found_scores.get(node.id, (None, 0))[0],
                health_points=found_scores.get(node.id, (None, 0))[1],
            )
        )
    return found


def _device_of(nodes, node) -> int:
    """往上找最近的设备节点 id（对象挂在设备下；找不到时返回 0）。"""
    by_id = {item.id: item for item in nodes}
    current = node
    while current is not None:
        if current.node_type == "device":
            return current.id
        current = by_id.get(current.parent_id) if current.parent_id else None
    return 0


# ---------------------------------------------------------------- 九步进度


def step_states(session: Session, task_id: int) -> list[tuple[str, str, str]]:
    """九步的完成情况：done / current（第一个未完成）/ todo。"""
    nodes = tree.list_nodes(session, task_id)
    ds_rows = dataset_store.list_datasets(session, task_id)
    usage = tasks.task_usage(session, task_id)
    scopes = _metric_scopes(session, task_id, nodes)
    counts = metric_store.series_counts(session, task_id, [item.series_scope() for item in scopes])

    done = {
        "①": True,
        "②": any(item.node_type in ("device", "component") for item in nodes),
        "③": bool(sensors.list_sensors(session, task_id)),
        "④": bool(metric_def.list_metrics(session, task_id)),
        "⑤": bool(ds_rows),
        "⑥": bool(ds_rows) and all(row.status != "待判" for row in ds_rows),
        "⑦": usage["metric_value"] > 0,
        "⑧": bool(limits.list_limits(session, task_id, nodes)),
        "⑨": any(count >= 2 for count in counts.values()),
    }

    states: list[tuple[str, str, str]] = []
    current_taken = False
    for code, name in STEPS:
        if done.get(code):
            states.append((code, name, "done"))
        elif not current_taken:
            states.append((code, name, "current"))
            current_taken = True
        else:
            states.append((code, name, "todo"))
    return states


# ---------------------------------------------------------------- 汇总


def task_overview(session: Session, task_id: int | None = None) -> TaskOverview:
    """一次算全工作台要显示的数字；没有任务时各计数为 0、不报错。"""
    task_count = len(tasks.list_tasks(session))
    empty_counts = {state: 0 for state in HEALTH_STATES}
    if task_id is None:
        return TaskOverview(
            task_id=None,
            task_count=task_count,
            dataset_count=0,
            channel_count=0,
            metric_value_count=0,
            over_records=[],
            health_counts=empty_counts,
            devices=[],
            objects=[],
            steps=[(code, name, "todo") for code, name in STEPS],
            pending_validity=0,
            stale_count=0,
        )

    usage = tasks.task_usage(session, task_id)
    ds_rows = dataset_store.list_datasets(session, task_id)
    health = node_health(session, task_id)
    return TaskOverview(
        task_id=task_id,
        task_count=task_count,
        dataset_count=usage["dataset"],
        channel_count=usage["channel"],
        metric_value_count=usage["metric_value"],
        over_records=over_records(session, task_id),
        health_counts=health_counts(session, task_id, health),
        devices=device_overviews(session, task_id, health),
        objects=object_overviews(session, task_id, health),
        steps=step_states(session, task_id),
        pending_validity=sum(1 for row in ds_rows if row.status == "待判"),
        stale_count=metric_store.count_stale(session, task_id),
    )


# ---------------------------------------------------------------- 最近批次


def recent_datasets(session: Session, task_id: int, limit: int = 3) -> list:
    """最近导入的数据集（采集时间新的在前）。

    2026-11-04：工作台「最近导入」只显示最近 3 条。
    """
    rows = dataset_store.list_datasets(session, task_id)
    rows.sort(key=lambda row: (row.collected_at, row.id))
    return list(reversed(rows))[:limit]
