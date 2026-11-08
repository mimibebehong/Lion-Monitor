"""节点健康状态：指标层按最新值判级，通道/对象/设备/任务沿子树汇总。

判定口径（方案 9.10 健康评级）：

- 指标节点：取该通道该指标**最近 5 个有效点**里最严重的一个 vs 继承阈值 → ok / warn / alert / unset
  （2026-11-03：只看 1 个点稳定性差；``stale`` 旧参数点不参与判级）
- 整条序列都是 ``stale``（待重算）或没有点时 → **unset + 待重算标记**，既不判正常也不判告警
  （复查 v2 问题 4：作废值顶着一个 alert 会把人引向设备排查）
- 上层节点：取子树里最严重的状态；``over_count`` 为子树内越限点数之和（**不含** stale 点），
  ``stale_count`` 为子树内待重算行数（口径同 ``store.metrics.count_stale``）

P3 / S2：最新值、越限条数在库里聚合（一条 GROUP BY + 一条窗口函数），
阈值一次取全量后在内存里沿父链继承 —— 不再逐节点 ``tree.path_nodes`` 查祖先链，
也不再调 ``series.metric_series`` 把整串点拉成 ValueRow。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .. import limits_rules
from ..store import limits, metrics, tree
from . import series

LEVEL_ORDER = {"unset": 0, "ok": 1, "invalid": 2, "warn": 3, "alert": 4}

#: 判级窗口：指标节点取最近 N 个点里最严重的一个（2026-11-03，1 个点稳定性差）
GRADE_WINDOW = 5


@dataclass(slots=True)
class NodeHealth:
    node_id: int
    state: str = "unset"
    latest_value: float | None = None
    latest_at: str = ""
    unit: str = ""
    #: 子树内越过 warn 的点数（**不含** stale 点）
    over_count: int = 0
    #: 子树内待重算（stale=1）的指标值行数，与趋势页「待重算 N 条」同一口径
    stale_count: int = 0
    #: True = 有数据但全是待重算点 / 没有可判级的有效点，状态只能是 unset + 「待重算」
    needs_recompute: bool = False


@dataclass(slots=True)
class _Agg:
    state: str = "unset"
    latest_value: float | None = None
    latest_at: str = ""
    unit: str = ""
    over_count: int = 0
    children: list[int] = field(default_factory=list)


def point_level(value: float, limit: tuple[float, float, str, str] | None) -> str:
    """单点判级：规则走 :func:`lion_monitor.limits_rules.grade`（方向口径单一来源）。

    返回值：``ok`` / ``warn`` / ``alert``；没设阈值 → ``unset``；
    **有阈值但值非有限（NaN/±Inf）→ ``invalid``**（页面文案「数据异常」，复查 G10）——
    这种点没法判级，也不许显示成绿色「正常」。
    """
    if limit is None:
        return "unset"
    warn, alert, direction, _ = limit
    if not warn and not alert:  # 历史遗留：两侧都 0 等于没设，不许判成「正常」
        return "unset"
    level = limits_rules.grade(value, direction, warn, alert)
    if level == "unset":  # grade 只在值非有限时返回 unset（没阈值已在上面返回）
        return "invalid"
    return "ok" if level == "normal" else level


def node_health(session: Session, task_id: int) -> dict[int, NodeHealth]:
    """算出整棵树每个节点的健康状态（指标节点用数据，其余节点向上汇总）。"""
    nodes = tree.list_nodes(session, task_id)
    health = {node.id: NodeHealth(node_id=node.id) for node in nodes}

    # 指标节点 → 它自己通道节点的取数范围（同通道多对象时各读各的，不再共用一串混值）
    scopes = series.metric_scopes(session, task_id, nodes)
    codes = {item.metric_node.id: item.code for item in scopes}
    resolved = limits.resolve_limits(session, task_id, codes, nodes=nodes)
    requests = [
        metrics.SeriesRequest(item.series_scope(), resolved.get(item.metric_node.id))
        for item in scopes
    ]
    stats = metrics.series_stats_batch(session, task_id, requests)
    window = metrics.recent_values_batch(
        session, task_id, [item.series_scope() for item in scopes], n=GRADE_WINDOW
    )
    stale_counts = metrics.stale_counts_batch(
        session, task_id, [item.series_scope() for item in scopes]
    )

    for item, found in zip(scopes, stats, strict=True):
        limit = resolved.get(item.metric_node.id)
        node_id = item.metric_node.id
        scope = item.series_scope()
        stale_n = stale_counts.get(scope, 0)
        if found.latest is None:
            # 没点，或整条序列都是待重算的旧参数点（latest 只取 stale=0 的行）：
            # 一律 unset；有 stale 行时挂「待重算」，不判正常也不判告警（复查 v2 问题 4）
            health[node_id] = NodeHealth(
                node_id=node_id,
                state="unset",
                stale_count=stale_n,
                needs_recompute=stale_n > 0,
            )
            continue
        # 判级窗口：最近 N 个点里取最严重；stale（旧参数算的）点不参与判级
        recent = window.get(scope, [])
        fresh = [row for row in recent if not row.stale]
        graded = fresh or [found.latest]  # 窗口内全是 stale 时用最近一个有效点兜底
        state = "unset"
        for row in graded:
            level = point_level(row.value, limit)
            if LEVEL_ORDER[level] > LEVEL_ORDER[state]:
                state = level
        health[node_id] = NodeHealth(
            node_id=node_id,
            state=state,
            latest_value=found.latest.value,
            latest_at=found.latest.computed_at,
            unit=found.latest.unit,
            over_count=found.over_count,
            stale_count=stale_n,
            needs_recompute=state == "unset" and stale_n > 0,
        )

    # 上层节点向上汇总：按 path 深度倒序（父在子后算），不查祖先链
    children: dict[int, list[int]] = {}
    for node in nodes:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)
    for node in sorted(nodes, key=lambda item: item.path.count("/"), reverse=True):
        if node.node_type == "metric":
            continue
        kids = children.get(node.id, [])
        if not kids:
            continue
        worst = "unset"
        over = 0
        stale = 0
        recompute = False
        latest_value = None
        latest_at = ""
        unit = ""
        for kid in kids:
            kh = health[kid]
            over += kh.over_count
            stale += kh.stale_count
            recompute = recompute or kh.needs_recompute
            if LEVEL_ORDER[kh.state] > LEVEL_ORDER[worst]:
                worst = kh.state
                latest_value = kh.latest_value
                latest_at = kh.latest_at
                unit = kh.unit
        health[node.id] = NodeHealth(
            node_id=node.id,
            state=worst,
            latest_value=latest_value,
            latest_at=latest_at,
            unit=unit,
            over_count=over,
            stale_count=stale,
            needs_recompute=recompute,
        )

    return health
