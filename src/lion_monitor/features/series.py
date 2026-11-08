"""指标值按节点取数（P2 / S1）：同一通道号挂多个监测对象时各读各的节点。

口径（Q1 允许同通道多对象 + 方案 A「值按节点存」）：

- 值按**通道节点**落库（``metric_value.node_id`` 是健康树里 channel 类型节点的 id）；
  一个指标节点只读「它所属通道节点」的那几行 —— 不再按通道号混读。
- 该通道名在本任务下只有一个通道节点时，历史无归属行（``node_id IS NULL``）一并带上：
  兼容老数据、不丢点；同名通道有多个节点时不带（无法归属，带进来还是串值）。
- 逐点序列按 (通道节点, 指标码) 缓存：同一通道节点下的多个指标各取各的码。

趋势页 / 健康评级 / 工作台超限记录都走这里，保证三处口径一致。

P3 / S2：这里只按「树结构」算范围（指标节点的父节点就是通道节点，层级固定），
不再逐节点调 ``tree.path_nodes`` 查祖先链；要数字的地方（计数 / 最新值 / 越限数）
直接用 :class:`lion_monitor.store.metrics.SeriesScope` 交给 SQL 聚合。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..store import metrics, tree

__all__ = [
    "MetricScope",
    "MetricSeries",
    "channel_nodes_by_name",
    "channel_of",
    "metric_scopes",
    "metric_series",
]


@dataclass(slots=True)
class MetricSeries:
    """一个指标节点对应的逐点序列：指标节点 + 所属通道节点 + 指标码 + 值行。"""

    metric_node: tree.NodeRow
    channel_name: str
    channel_node_id: int
    code: str
    values: list[metrics.ValueRow]


@dataclass(frozen=True, slots=True)
class MetricScope:
    """一个指标节点的取数范围（不拉值）：指标节点 + 所属通道节点 + 指标码。"""

    metric_node: tree.NodeRow
    channel_node: tree.NodeRow
    code: str
    include_unattributed: bool = False

    def series_scope(self) -> metrics.SeriesScope:
        """交给 store 层做 SQL 聚合时用的范围。"""
        return metrics.SeriesScope(
            code=self.code,
            channel_code=self.channel_node.name,
            channel_node_id=self.channel_node.id,
            include_unattributed=self.include_unattributed,
        )


def channel_nodes_by_name(
    session: Session, task_id: int, nodes: list[tree.NodeRow] | None = None
) -> dict[str, list[int]]:
    """通道名 → 该任务下同名的通道节点 id 列表（Q1 允许同一通道号挂多个对象）。"""
    nodes = nodes if nodes is not None else tree.list_nodes(session, task_id)
    found: dict[str, list[int]] = {}
    for node in nodes:
        if node.node_type == "channel":
            found.setdefault(node.name, []).append(node.id)
    return found


def channel_of(session: Session, node: tree.NodeRow) -> tree.NodeRow | None:
    """指标节点往上找所属通道节点（层级固定：指标的父节点就是通道）。"""
    if node.node_type != "metric" or node.parent_id is None:
        return None
    parent = tree.get_node(session, node.parent_id)
    return parent if parent.node_type == "channel" else None


def metric_scopes(
    session: Session, task_id: int, nodes: list[tree.NodeRow] | None = None
) -> list[MetricScope]:
    """任务下每个指标节点的取数范围（只读一次树，不查祖先链、不拉值）。"""
    nodes = nodes if nodes is not None else tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in nodes}
    counts: dict[str, int] = {}
    for node in nodes:
        if node.node_type == "channel":
            counts[node.name] = counts.get(node.name, 0) + 1

    found: list[MetricScope] = []
    for node in nodes:
        if node.node_type != "metric":
            continue
        parent = by_id.get(node.parent_id) if node.parent_id is not None else None
        if parent is None or parent.node_type != "channel":
            continue
        code = (node.attrs or {}).get("code") or node.name
        found.append(
            MetricScope(
                metric_node=node,
                channel_node=parent,
                code=code,
                include_unattributed=counts.get(parent.name, 0) <= 1,
            )
        )
    return found


def metric_series(
    session: Session, task_id: int, nodes: list[tree.NodeRow] | None = None
) -> list[MetricSeries]:
    """任务下每个指标节点的逐点序列，只取它自己通道节点的行。"""
    cache: dict[tuple[int, str, bool], list[metrics.ValueRow]] = {}
    found: list[MetricSeries] = []
    for item in metric_scopes(session, task_id, nodes):
        key = (item.channel_node.id, item.code, item.include_unattributed)
        if key not in cache:
            cache[key] = metrics.list_values(
                session,
                task_id,
                item.channel_node.name,
                item.code,
                node_id=item.channel_node.id,
                include_unattributed=item.include_unattributed,
            )
        found.append(
            MetricSeries(
                metric_node=item.metric_node,
                channel_name=item.channel_node.name,
                channel_node_id=item.channel_node.id,
                code=item.code,
                values=cache[key],
            )
        )
    return found
