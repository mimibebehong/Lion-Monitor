"""指标值读写（趋势分析的数据源）：一个通道节点 × 一次计算 × 一个指标 = 一行。

口径（方案 A，马哥 2026-10-31 拍板）：**值按节点存**——``node_id`` 是健康树通道节点 id；
同一通道号挂在多个监测对象下时各按自己节点的生效参数算、各存一行；历史行 ``node_id`` 为空。
底层沿用脚手架的 ``metric_value`` 表（channel_id / code / value / unit / computed_at）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, delete, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from .. import limits_rules
from .models import Channel, Dataset, MetricValue

__all__ = [
    "FINITE_MAX",
    "LEGACY_NODE_KEY",
    "NodeValue",
    "SeriesRequest",
    "SeriesScope",
    "SeriesStats",
    "ValueRow",
    "count_values_for_dataset",
    "count_stale",
    "delete_values_for_channel",
    "delete_values_for_dataset",
    "list_values",
    "mark_stale",
    "over_rows_for_scopes",
    "over_values",
    "over_values_for_scopes",
    "replace_node_series",
    "series_counts",
    "series_stats",
    "series_stats_batch",
    "stale_counts_batch",
    "trend_series",
    "upsert_node_values",
    "upsert_values",
]

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_TIME_FMT_US = "%Y-%m-%d %H:%M:%S.%f"


def _parse_moment(text: str) -> datetime:
    """解析时间戳：整秒（历史行）与带微秒（S4W-1 亚秒步长的窗点）两种格式都认。"""
    for fmt in (_TIME_FMT, _TIME_FMT_US):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间格式不合法（应为 YYYY-mm-dd HH:MM:SS[.ffffff]）：{text}")


def _moment_text(moment: datetime | None) -> str:
    """时间戳转文本：整秒沿用历史格式，带微秒时保留微秒（逐窗亚秒点不互相覆盖）。"""
    if moment is None:
        return ""
    return moment.strftime(_TIME_FMT_US) if moment.microsecond else moment.strftime(_TIME_FMT)


#: 无归属历史行（``node_id IS NULL``）在 SQL 分组里的合并键（真实节点 id 都是正数）
LEGACY_NODE_KEY = -1

#: 有限值边界：±Inf 不参与比较（与 :func:`limits_rules.grade` 口径一致）
FINITE_MAX = sys.float_info.max


@dataclass(slots=True)
class NodeValue:
    """一行待落库的指标值：算它的节点 + 指标码 + 值 + 参数快照（追溯用）。"""

    node_id: int | None
    code: str
    value: float
    unit: str = ""
    params_hash: str = ""
    effective_params_json: str = ""
    rpm_used: float | None = None
    rpm_source: str = ""
    stale: bool = False


@dataclass(slots=True)
class ValueRow:
    id: int
    channel_id: int
    channel_code: str
    dataset_id: int
    metric_code: str
    value: float
    unit: str
    computed_at: str
    params_hash: str = ""
    rpm_used: float | None = None
    rpm_source: str = ""
    stale: bool = False
    node_id: int | None = None


def _to_row(model: MetricValue, channel_code: str = "", dataset_id: int = 0) -> ValueRow:
    return ValueRow(
        id=model.id,
        channel_id=model.channel_id,
        channel_code=channel_code,
        dataset_id=dataset_id,
        metric_code=model.code,
        value=float(model.value),
        unit=model.unit,
        computed_at=_moment_text(model.computed_at),
        params_hash=model.params_hash or "",
        rpm_used=float(model.rpm_used) if model.rpm_used is not None else None,
        rpm_source=model.rpm_source or "",
        stale=bool(model.stale),
        node_id=model.node_id,
    )


def _hash_of(code: str, param_hashes: dict[str, str] | str | None) -> str:
    """本次写入该指标行的参数指纹：可按指标码分别给，也可一份给全部。"""
    if param_hashes is None:
        return ""
    if isinstance(param_hashes, str):
        return param_hashes
    return param_hashes.get(code, "")


def upsert_node_values(
    session: Session,
    channel_id: int,
    computed_at: str,
    records: Sequence[NodeValue],
) -> int:
    """按节点落库：同一通道 × 同一时间点的旧值先删后写，重复导入不会翻倍。

    一个通道号可能挂多个节点（各存一行），所以整点先清空再一次性写入。

    **只 flush 不 commit**：批次导入的「建数据集 + 建通道 + 写值 + 落状态」必须
    在同一个事务里（P1 / B2），事务边界由 :func:`features.pipeline.import_batch` 持有。
    """
    moment = _parse_moment(computed_at)
    session.execute(
        delete(MetricValue).where(
            MetricValue.channel_id == channel_id,
            MetricValue.computed_at == moment,
        )
    )
    session.flush()
    for record in records:
        session.add(
            MetricValue(
                channel_id=channel_id,
                node_id=record.node_id,
                code=record.code,
                value=float(record.value),
                unit=record.unit,
                level="unset",
                computed_at=moment,
                params_hash=record.params_hash,
                effective_params_json=record.effective_params_json or "",
                rpm_used=float(record.rpm_used) if record.rpm_used is not None else None,
                rpm_source=record.rpm_source or "",
                stale=1 if record.stale else 0,
            )
        )
    session.flush()
    return len(records)


def upsert_values(
    session: Session,
    channel_id: int,
    values: dict[str, float],
    units: dict[str, str],
    computed_at: str,
    param_hashes: dict[str, str] | str | None = None,
    rpm_used: float | None = None,
    rpm_source: str = "",
    stale: bool = False,
    node_id: int | None = None,
) -> int:
    """老接口：一份值写一个节点（``node_id=None`` = 历史口径 / 不挂树的写入）。

    ``param_hashes``/``rpm_used``/``rpm_source``/``stale`` 是参数快照（追溯用），
    不传时写空值，行为与改造前一致。
    """
    records = [
        NodeValue(
            node_id=node_id,
            code=code,
            value=value,
            unit=units.get(code, ""),
            params_hash=_hash_of(code, param_hashes),
            rpm_used=rpm_used,
            rpm_source=rpm_source,
            stale=stale,
        )
        for code, value in values.items()
    ]
    return upsert_node_values(session, channel_id, computed_at, records)


def list_values(
    session: Session,
    task_id: int,
    channel_code: str,
    metric_code: str | None = None,
    node_id: int | None = None,
    include_unattributed: bool = False,
    since: str | datetime | None = None,
) -> list[ValueRow]:
    """按通道号取该任务下的指标值（跨数据集与节点，按时间升序）。

    ``node_id`` 给了就只取该节点的行（方案 A 下同一通道号可能有多份）；不给 = 全部，兼容老调用。

    ``include_unattributed=True`` 时把**无归属历史行**（``node_id IS NULL``，方案 A 之前落的）
    一并带上——只在该通道名对应唯一节点时这么用（上层口径见 ``features.series``），
    否则老行会被每个节点各读一份，又变成串值。

    ``since`` 给了就只取不早于该时刻的行（趋势页默认 30 天窗口用，见 Q6）。
    """
    stmt = (
        select(MetricValue, Channel, Dataset)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Channel.code == channel_code,
            Dataset.deleted_at.is_(None),
            Channel.deleted_at.is_(None),
            MetricValue.deleted_at.is_(None),
        )
    )
    if metric_code is not None:
        stmt = stmt.where(MetricValue.code == metric_code)
    if node_id is not None:
        if include_unattributed:
            stmt = stmt.where(or_(MetricValue.node_id == node_id, MetricValue.node_id.is_(None)))
        else:
            stmt = stmt.where(MetricValue.node_id == node_id)
    if since is not None:
        moment = since if isinstance(since, datetime) else _parse_moment(since)
        stmt = stmt.where(MetricValue.computed_at >= moment)
    stmt = stmt.order_by(MetricValue.computed_at, MetricValue.id)
    return [_to_row(v, ch.code, d.id) for v, ch, d in session.execute(stmt)]


def trend_series(
    session: Session,
    task_id: int,
    channel_code: str,
    metric_code: str,
    node_id: int | None = None,
) -> list[tuple[str, float]]:
    """逐点趋势（不聚合）：[(时间, 值), ...] 按时间升序。"""
    return [
        (row.computed_at, row.value)
        for row in list_values(session, task_id, channel_code, metric_code, node_id=node_id)
    ]


# ---------------------------------------------------------------- SQL 聚合（P3 / S2）
#
# 工作台 / 健康树只要「计数、最新值、越限条数」和「越限那几行」，不再把整串点拉成
# ValueRow。口径与逐点判级一致（``limits_rules.grade`` + ``features.health.point_level``）：
# 先判 alert（命中即 alert），±Inf 这类非有限值不参与比较。


@dataclass(frozen=True, slots=True)
class SeriesScope:
    """一条序列的取数范围（P2 / S1 口径）：通道节点 × 指标码。

    ``channel_node_id=None`` 表示不按节点过滤（老调用 / 只按通道号取数）；
    ``include_unattributed=True`` 时把该通道码下 ``node_id IS NULL`` 的历史行一并算进来
    （只在该通道名对应唯一节点时用，否则老行会被多个节点各读一份）。
    """

    code: str
    channel_code: str
    channel_node_id: int | None = None
    include_unattributed: bool = False


@dataclass(slots=True)
class SeriesStats:
    """一条序列的库里算出来的数字：行数、越限数、最新一行。"""

    count: int = 0
    warn_count: int = 0
    alert_count: int = 0
    latest: ValueRow | None = None

    @property
    def over_count(self) -> int:
        return self.warn_count + self.alert_count


@dataclass(frozen=True, slots=True)
class SeriesRequest:
    """一条序列 + 它自己的生效阈值（同一通道节点下多个指标节点的阈值可能不同）。"""

    scope: SeriesScope
    limit: tuple[float, float, str, str] | None = None


def _value_condition(kind: str, limit: tuple[float, float, str, str]):
    """``kind="warn"`` 越过 warn（含 alert）；``kind="alert"`` 越过 alert；非有限值不算。

    复查 v2 问题 4：``stale=1``（旧参数算出的待重算点）**不参与越限统计**——判级与
    ``over_count`` 都只看当前口径的点，否则作废的值会一直顶着一个 alert。
    """
    warn, alert, direction, _source = limit
    finite = and_(MetricValue.value <= FINITE_MAX, MetricValue.value >= -FINITE_MAX)
    fresh = MetricValue.stale == 0
    threshold = warn if kind == "warn" else alert
    if limits_rules.normalize_direction(direction) == "lower":
        return and_(finite, fresh, MetricValue.value <= threshold)
    return and_(finite, fresh, MetricValue.value >= threshold)


def _scope_where(task_id: int, scopes: Sequence[SeriesScope]):
    """序列范围 → WHERE：任务 + 未删 + 指标码 + （节点 in / 通道码 in / 无归属历史行）。"""
    codes = sorted({scope.code for scope in scopes})
    node_ids = sorted(
        {scope.channel_node_id for scope in scopes if scope.channel_node_id is not None}
    )
    loose_codes = sorted({scope.channel_code for scope in scopes if scope.channel_node_id is None})
    legacy_codes = sorted({scope.channel_code for scope in scopes if scope.include_unattributed})

    wanted = []
    if node_ids:
        wanted.append(MetricValue.node_id.in_(node_ids))
    if loose_codes:
        wanted.append(Channel.code.in_(loose_codes))
    if legacy_codes:
        wanted.append(and_(MetricValue.node_id.is_(None), Channel.code.in_(legacy_codes)))
    return and_(
        Dataset.task_id == task_id,
        Dataset.deleted_at.is_(None),
        Channel.deleted_at.is_(None),
        MetricValue.deleted_at.is_(None),
        MetricValue.code.in_(codes),
        or_(*wanted),
    )


def _key_exprs() -> tuple:
    """SQL 分组键：节点（无归属历史行合并成 LEGACY_NODE_KEY）、通道码、指标码。"""
    return (
        func.coalesce(MetricValue.node_id, LEGACY_NODE_KEY),
        Channel.code,
        MetricValue.code,
    )


def _count_query(task_id: int, scopes: Sequence[SeriesScope], limit=None):
    """计数（有阈值时连带 warn / alert 命中数）+ 每组最新时刻——一条 GROUP BY 语句。

    最新时刻顺手在这里算出来（``max(computed_at)``）：取最新行时只按
    (节点, 指标码, 时刻) 回取那几行（见 ``_latest_rows_query``），不再为「最新值」
    单独跑一条 ``row_number()`` 窗口函数——那条语句在 24 万行时要把整表排序。

    ``latest_at`` 只取 ``stale=0`` 的行（复查 v2 问题 4）：判级要「最近一个有效点」，
    不能拿到一个待重算的旧参数点；整条序列都是 stale 时给它 NULL，由调用方判「待重算」。
    """
    keys = _key_exprs()
    cols = [
        keys[0].label("node_key"),
        keys[1].label("channel_code"),
        keys[2].label("metric_code"),
        func.count().label("rows"),
        func.max(case((MetricValue.stale == 0, MetricValue.computed_at))).label("latest_at"),
    ]
    if limit is not None:
        cols.append(
            func.sum(case((_value_condition("warn", limit), 1), else_=0)).label("warn_hits")
        )
        cols.append(
            func.sum(case((_value_condition("alert", limit), 1), else_=0)).label("alert_hits")
        )
    stmt = (
        select(*cols)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(_scope_where(task_id, scopes))
    )
    return stmt.group_by(*keys)


def _latest_rows_query(task_id: int, stamps: Sequence[tuple[int, str, str, datetime]]):
    """按 (节点键, 通道码, 指标码, 时刻) 回取最新行——组数就是序列数，不是点数。

    ``stamps`` 来自 ``_count_query`` 的 ``latest_at``；无归属历史行（``node_id`` 为空）
    只能按通道码定位，所以分两条条件走。同刻多行（跨批次 / 重复写入）都取回来，
    由调用方按 (时刻, id) 取最大值，与原来窗口函数的 ``order by computed_at, id`` 一致。
    """
    node_triples = [
        (node_key, code, stamp)
        for node_key, _channel_code, code, stamp in stamps
        if node_key != LEGACY_NODE_KEY
    ]
    legacy_triples = [
        (channel_code, code, stamp)
        for node_key, channel_code, code, stamp in stamps
        if node_key == LEGACY_NODE_KEY
    ]
    wanted = []
    if node_triples:
        wanted.append(
            tuple_(MetricValue.node_id, MetricValue.code, MetricValue.computed_at).in_(node_triples)
        )
    if legacy_triples:
        wanted.append(
            and_(
                MetricValue.node_id.is_(None),
                tuple_(Channel.code, MetricValue.code, MetricValue.computed_at).in_(legacy_triples),
            )
        )
    return (
        select(
            MetricValue.id.label("id"),
            MetricValue.channel_id.label("channel_id"),
            MetricValue.node_id.label("node_id"),
            func.coalesce(MetricValue.node_id, LEGACY_NODE_KEY).label("node_key"),
            Channel.code.label("channel_code"),
            Dataset.id.label("dataset_id"),
            MetricValue.code.label("metric_code"),
            MetricValue.value.label("value"),
            MetricValue.unit.label("unit"),
            MetricValue.computed_at.label("computed_at"),
            MetricValue.params_hash.label("params_hash"),
            MetricValue.rpm_used.label("rpm_used"),
            MetricValue.rpm_source.label("rpm_source"),
            MetricValue.stale.label("stale"),
        )
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Dataset.deleted_at.is_(None),
            Channel.deleted_at.is_(None),
            MetricValue.deleted_at.is_(None),
            or_(*wanted),
        )
    )


def _lookups(scopes: Sequence[SeriesScope]):
    """SQL 分组键 → 序列范围的三种映射（按节点 / 无归属历史行 / 只按通道码）。"""
    by_node: dict[tuple[int, str], SeriesScope] = {}
    by_legacy: dict[tuple[str, str], SeriesScope] = {}
    by_channel: dict[tuple[str, str], SeriesScope] = {}
    for scope in scopes:
        if scope.channel_node_id is not None:
            by_node[(scope.channel_node_id, scope.code)] = scope
        if scope.include_unattributed:
            by_legacy[(scope.channel_code, scope.code)] = scope
        if scope.channel_node_id is None:
            by_channel[(scope.channel_code, scope.code)] = scope
    return by_node, by_legacy, by_channel


def _pick_scope(row, lookups) -> SeriesScope | None:
    by_node, by_legacy, by_channel = lookups
    if row.node_key != LEGACY_NODE_KEY:
        scope = by_node.get((row.node_key, row.metric_code))
    else:
        scope = by_legacy.get((row.channel_code, row.metric_code))
    if scope is None:
        # 没按节点过滤的范围（老调用 / 只按通道码取数）：该通道码上的行都算它的
        scope = by_channel.get((row.channel_code, row.metric_code))
    return scope


def _to_value_row(record) -> ValueRow:
    """把聚合 / 最新值回取 / 越限查询的一行（列名见 ``_latest_rows_query``）拼成 ValueRow。"""
    return ValueRow(
        id=record.id,
        channel_id=record.channel_id,
        channel_code=record.channel_code,
        dataset_id=record.dataset_id,
        metric_code=record.metric_code,
        value=float(record.value),
        unit=record.unit,
        computed_at=_moment_text(record.computed_at),
        params_hash=record.params_hash or "",
        rpm_used=float(record.rpm_used) if record.rpm_used is not None else None,
        rpm_source=record.rpm_source or "",
        stale=bool(record.stale),
        node_id=record.node_id,
    )


def series_counts(session: Session, task_id: int, scopes: Sequence[SeriesScope]) -> dict:
    """每条序列的行数（一条 GROUP BY 语句），给「趋势 = 至少两个点」这类判断用。"""
    unique = list(dict.fromkeys(scopes))
    if not unique:
        return {}
    lookups = _lookups(unique)
    found = {scope: 0 for scope in unique}
    for row in session.execute(_count_query(task_id, unique)):
        scope = _pick_scope(row, lookups)
        if scope is not None:
            found[scope] += int(row.rows or 0)
    return found


def series_stats_batch(
    session: Session, task_id: int, requests: Sequence[SeriesRequest]
) -> list[SeriesStats]:
    """批量算计数 / 越限数 / 最新值，返回与 ``requests`` 一一对应的结果。

    语句数：同一阈值 → 计数一条（顺带算出每组最新时刻）+ 按最新时刻回取一条；
    阈值不同 → 计数一条 + 每个阈值一条 + 按最新时刻回取一条。
    两条都只随序列数增长，不随点数增长，也不按节点逐条查。
    """
    wanted = list(dict.fromkeys(requests))
    if not wanted:
        return []

    scopes = list(dict.fromkeys(request.scope for request in wanted))
    limits_used = list(dict.fromkeys(request.limit for request in wanted))
    lookups = _lookups(scopes)
    counts: dict[SeriesScope, int] = {scope: 0 for scope in scopes}
    hits: dict[tuple[SeriesScope, object], list[int]] = {
        (request.scope, request.limit): [0, 0] for request in wanted
    }
    #: 每组（节点键, 通道码, 指标码）的最新时刻——聚合语句顺手给的，稍后按它回取最新行
    stamps: dict[tuple[int, str, str], datetime] = {}

    def _run(rows, key_limit, count_rows: bool) -> None:
        for row in rows:
            scope = _pick_scope(row, lookups)
            if scope is None:
                continue
            if count_rows:
                counts[scope] += int(row.rows or 0)
                if row.latest_at is not None:
                    stamps[(row.node_key, row.channel_code, row.metric_code)] = row.latest_at
            bucket = hits.get((scope, key_limit))
            if bucket is not None:
                bucket[0] += int(getattr(row, "warn_hits", 0) or 0)
                bucket[1] += int(getattr(row, "alert_hits", 0) or 0)

    if len(limits_used) == 1:
        # 阈值一致（或都没阈值）：计数与越限命中合成一条语句
        _run(session.execute(_count_query(task_id, scopes, limits_used[0])), limits_used[0], True)
    else:
        _run(session.execute(_count_query(task_id, scopes, None)), None, True)
        for limit in limits_used:
            if limit is None:
                continue
            bucket_scopes = [
                scope
                for scope in scopes
                if any(request.scope == scope and request.limit == limit for request in wanted)
            ]
            _run(session.execute(_count_query(task_id, bucket_scopes, limit)), limit, False)

    latest: dict[SeriesScope, ValueRow] = {}
    if stamps:
        keys = [
            (node_key, channel_code, code, stamp)
            for (node_key, channel_code, code), stamp in stamps.items()
        ]
        for record in session.execute(_latest_rows_query(task_id, keys)):
            scope = _pick_scope(record, lookups)
            if scope is None:
                continue
            row = _to_value_row(record)
            kept = latest.get(scope)
            if kept is None or (row.computed_at, row.id) >= (kept.computed_at, kept.id):
                latest[scope] = row

    resolved: dict[SeriesRequest, SeriesStats] = {}
    for request in wanted:
        warn_ge, alert_ge = hits[(request.scope, request.limit)]
        resolved[request] = SeriesStats(
            count=counts[request.scope],
            warn_count=max(warn_ge - alert_ge, 0),
            alert_count=alert_ge,
            latest=latest.get(request.scope),
        )
    return [resolved[request] for request in requests]


def recent_values_batch(
    session: Session, task_id: int, scopes: Sequence[SeriesScope], n: int = 5
) -> dict[SeriesScope, list[ValueRow]]:
    """每条序列最近 ``n`` 个点（新→旧），给「按最近 N 点判级」用。

    一条 ``row_number()`` 窗口函数语句：组数 = 序列数，回取行数 ≤ 序列数 × n，
    不随历史点数增长（``series_stats_batch`` 的聚合走另一条语句，两者互不影响）。
    """
    unique = list(dict.fromkeys(scopes))
    if not unique or n <= 0:
        return {}
    lookups = _lookups(unique)
    keys = _key_exprs()
    rank = (
        func.row_number()
        .over(
            partition_by=list(keys),
            order_by=(MetricValue.computed_at.desc(), MetricValue.id.desc()),
        )
        .label("rn")
    )
    inner = (
        select(
            MetricValue.id.label("id"),
            MetricValue.channel_id.label("channel_id"),
            Channel.code.label("channel_code"),
            Channel.dataset_id.label("dataset_id"),
            MetricValue.code.label("metric_code"),
            MetricValue.value.label("value"),
            MetricValue.unit.label("unit"),
            MetricValue.computed_at.label("computed_at"),
            MetricValue.params_hash.label("params_hash"),
            MetricValue.rpm_used.label("rpm_used"),
            MetricValue.rpm_source.label("rpm_source"),
            MetricValue.stale.label("stale"),
            MetricValue.node_id.label("node_id"),
            keys[0].label("node_key"),
            rank,
        )
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(_scope_where(task_id, unique))
        .subquery()
    )
    found: dict[SeriesScope, list[ValueRow]] = {}
    stmt = select(inner).where(inner.c.rn <= n).order_by(inner.c.node_key, inner.c.rn)
    for record in session.execute(stmt):
        scope = _pick_scope(record, lookups)
        if scope is None:
            continue
        found.setdefault(scope, []).append(_to_value_row(record))
    return found


def series_stats(
    session: Session,
    task_id: int,
    channel_code: str,
    metric_code: str,
    node_id: int | None = None,
    include_unattributed: bool = False,
    limit: tuple[float, float, str, str] | None = None,
) -> SeriesStats:
    """单条序列的计数 / 越限数 / 最新值（两条语句：聚合 + 最新值）。"""
    scope = SeriesScope(
        code=metric_code,
        channel_code=channel_code,
        channel_node_id=node_id,
        include_unattributed=include_unattributed,
    )
    return series_stats_batch(session, task_id, [SeriesRequest(scope, limit)])[0]


def over_rows_for_scopes(
    session: Session,
    task_id: int,
    scopes: Sequence[SeriesScope],
    limit: tuple[float, float, str, str],
):
    """只取越过 warn 的行（含 alert），只带页面要的那几列，按计算时间升序。

    给工作台超限记录用：比 ``over_values_for_scopes`` 少 7 列、也不逐行拼 ``ValueRow``。
    越限记录多的时候（24 万行 / 4 万条越限）这一步的 Python 开销就是工作台的大头。
    """
    unique = list(dict.fromkeys(scopes))
    if not unique:
        return []
    stmt = (
        select(
            MetricValue.node_id.label("node_id"),
            Channel.code.label("channel_code"),
            MetricValue.code.label("metric_code"),
            MetricValue.value.label("value"),
            MetricValue.unit.label("unit"),
            MetricValue.computed_at.label("computed_at"),
        )
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(_scope_where(task_id, unique), _value_condition("warn", limit))
        .order_by(MetricValue.computed_at, MetricValue.id)
    )
    return session.execute(stmt)


def over_values_for_scopes(
    session: Session,
    task_id: int,
    scopes: Sequence[SeriesScope],
    limit: tuple[float, float, str, str],
) -> list[ValueRow]:
    """只取越过 warn 的行（含 alert），按计算时间升序——超限记录用，不拉全量。"""
    unique = list(dict.fromkeys(scopes))
    if not unique:
        return []
    stmt = (
        select(
            MetricValue.id.label("id"),
            MetricValue.channel_id.label("channel_id"),
            MetricValue.node_id.label("node_id"),
            Channel.code.label("channel_code"),
            Dataset.id.label("dataset_id"),
            MetricValue.code.label("metric_code"),
            MetricValue.value.label("value"),
            MetricValue.unit.label("unit"),
            MetricValue.computed_at.label("computed_at"),
            MetricValue.params_hash.label("params_hash"),
            MetricValue.rpm_used.label("rpm_used"),
            MetricValue.rpm_source.label("rpm_source"),
            MetricValue.stale.label("stale"),
        )
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(_scope_where(task_id, unique), _value_condition("warn", limit))
        .order_by(MetricValue.computed_at, MetricValue.id)
    )
    return [_to_value_row(record) for record in session.execute(stmt)]


def over_values(
    session: Session,
    task_id: int,
    channel_code: str,
    metric_code: str,
    node_id: int | None = None,
    include_unattributed: bool = False,
    limit: tuple[float, float, str, str] | None = None,
) -> list[ValueRow]:
    """单条序列的越限行（必须给阈值，否则无从判断哪些点越限）。"""
    if limit is None:
        raise ValueError("越限查询必须给阈值（limit），否则无法判断哪些点越限")
    scope = SeriesScope(
        code=metric_code,
        channel_code=channel_code,
        channel_node_id=node_id,
        include_unattributed=include_unattributed,
    )
    return over_values_for_scopes(session, task_id, [scope], limit)


def mark_stale(session: Session, node_id: int, metric_code: str | None = None) -> int:
    """把某节点（可限定指标）的旧值置 ``stale=1``，返回本次标记的行数。

    参数或转速基准改了以后旧值就过期了；本次只标记，重算链路（S4 之后）再生成新行。
    """
    stmt = (
        update(MetricValue)
        .where(
            MetricValue.node_id == node_id,
            MetricValue.stale == 0,
            MetricValue.deleted_at.is_(None),
        )
        .values(stale=1)
    )
    if metric_code is not None:
        stmt = stmt.where(MetricValue.code == metric_code)
    changed = session.execute(stmt).rowcount or 0
    session.commit()
    return changed


def _stale_where(task_id: int):
    """「待重算」行的公共 WHERE（``count_stale`` 与 ``stale_counts_batch`` 共用一份口径）。"""
    return (
        Dataset.task_id == task_id,
        Dataset.deleted_at.is_(None),
        Channel.deleted_at.is_(None),
        MetricValue.deleted_at.is_(None),
        MetricValue.stale == 1,
    )


def count_stale(
    session: Session,
    task_id: int,
    node_id: int | None = None,
    code: str | None = None,
) -> int:
    """该任务（可限定节点 / 指标码）下 ``stale=1`` 且未删的指标值行数。

    「待重算 N 条」的唯一口径：趋势页图注、评级页 / 工作台的计数、重算按钮的
    确认提示都读这一个函数，绝不各自写一遍 SQL。
    """
    stmt = (
        select(func.count())
        .select_from(MetricValue)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(*_stale_where(task_id))
    )
    if node_id is not None:
        stmt = stmt.where(MetricValue.node_id == node_id)
    if code is not None:
        stmt = stmt.where(MetricValue.code == code)
    return int(session.execute(stmt).scalar_one())


def stale_counts_batch(
    session: Session, task_id: int, scopes: Sequence[SeriesScope]
) -> dict[SeriesScope, int]:
    """批量版 :func:`count_stale`：``{范围: 待重算行数}``，一条 GROUP BY 语句。

    口径与 ``count_stale`` 完全一致（共用 ``_stale_where``），健康树判级要「整条序列都是
    待重算点时给 unset + 待重算」时按序列取数用，避免逐节点查库。
    """
    unique = [scope for scope in dict.fromkeys(scopes) if scope.channel_node_id is not None]
    if not unique:
        return {}
    found = {(scope.channel_node_id, scope.code): 0 for scope in unique}
    stmt = (
        select(
            MetricValue.node_id.label("node_id"),
            MetricValue.code.label("metric_code"),
            func.count().label("rows"),
        )
        .select_from(MetricValue)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            *_stale_where(task_id),
            MetricValue.node_id.in_([key[0] for key in found]),
            MetricValue.code.in_([key[1] for key in found]),
        )
        .group_by(MetricValue.node_id, MetricValue.code)
    )
    for row in session.execute(stmt):
        key = (int(row.node_id), row.metric_code)
        if key in found:
            found[key] = int(row.rows or 0)
    return {scope: found[(scope.channel_node_id, scope.code)] for scope in unique}


def replace_node_series(
    session: Session,
    channel_id: int,
    node_id: int,
    code: str,
    points: Sequence[tuple[str, NodeValue]],
) -> tuple[int, int]:
    """按当前参数重算的结果覆盖该 ``(通道, 节点, 指标)`` 的点位，返回 (写入, 作废)。

    口径（S4A，马哥拍板）：

    - 同一 ``computed_at`` 已有行 → **原地改写**值 / 参数快照 / ``stale=0``（id 不变，
      与导入「同一时点覆盖」口径一致）；
    - 本次新出现的时点 → 新增行；
    - 本次**没有产生**、且 ``stale=1`` 的旧点位（窗长 / 步长改了以后不再存在）→
      **软删**留痕：不能一直挂在「待重算」上，否则 N 永远清不到 0；
    - **逐窗前的老整段单值**（``node_id IS NULL``，同通道同指标）→ 一并软删：
      它们不属于任何节点，重算原先按节点找旧点永远碰不到，会残留成趋势里
      卡在记录起点的多余点（马哥 2026-11-02 确认）；
    - 非 stale 的行、别的指标、别的节点一律不动。

    ``points`` 里每个 ``NodeValue`` 的 ``node_id``/``code`` 由入参决定（记录里的同名字段
    只作携带，落库以 ``node_id``/``code`` 参数为准）。**只 flush 不 commit**。
    """
    produced: dict[datetime, NodeValue] = {}
    for stamp, record in points:
        produced[_parse_moment(stamp)] = record

    existing = {
        model.computed_at: model
        for model in session.execute(
            select(MetricValue).where(
                MetricValue.channel_id == channel_id,
                MetricValue.node_id == node_id,
                MetricValue.code == code,
                MetricValue.deleted_at.is_(None),
            )
        ).scalars()
    }

    written = 0
    for moment, record in produced.items():
        model = existing.get(moment)
        if model is None:
            session.add(
                MetricValue(
                    channel_id=channel_id,
                    node_id=node_id,
                    code=code,
                    value=float(record.value),
                    unit=record.unit,
                    level="unset",
                    computed_at=moment,
                    params_hash=record.params_hash,
                    effective_params_json=record.effective_params_json or "",
                    rpm_used=float(record.rpm_used) if record.rpm_used is not None else None,
                    rpm_source=record.rpm_source or "",
                    stale=0,
                )
            )
        else:
            model.value = float(record.value)
            model.unit = record.unit
            model.params_hash = record.params_hash
            model.effective_params_json = record.effective_params_json or ""
            model.rpm_used = float(record.rpm_used) if record.rpm_used is not None else None
            model.rpm_source = record.rpm_source or ""
            model.stale = 0
        written += 1

    retired = 0
    now = datetime.now()
    for moment, model in existing.items():
        if moment in produced or not model.stale:
            continue
        model.deleted_at = now
        retired += 1

    # 逐窗前的老整段单值（node_id 为空）由这次重算取代：不软删就会在趋势里多出旧点
    legacy = session.execute(
        select(MetricValue).where(
            MetricValue.channel_id == channel_id,
            MetricValue.node_id.is_(None),
            MetricValue.code == code,
            MetricValue.deleted_at.is_(None),
        )
    ).scalars()
    for model in legacy:
        model.deleted_at = now
        retired += 1

    session.flush()
    return written, retired


def count_values_for_dataset(session: Session, dataset_id: int) -> int:
    """该数据集下仍然有效的指标值行数（覆盖导入前提示「将覆盖 N 行」用）。"""
    stmt = (
        select(func.count())
        .select_from(MetricValue)
        .join(Channel, MetricValue.channel_id == Channel.id)
        .where(
            Channel.dataset_id == dataset_id,
            Channel.deleted_at.is_(None),
            MetricValue.deleted_at.is_(None),
        )
    )
    return int(session.execute(stmt).scalar_one())


def delete_values_for_channel(session: Session, channel_id: int) -> int:
    rows = list(
        session.execute(select(MetricValue).where(MetricValue.channel_id == channel_id)).scalars()
    )
    for model in rows:
        model.deleted_at = datetime.now()
    session.commit()
    return len(rows)


def delete_values_for_dataset(session: Session, dataset_id: int) -> int:
    channels = [
        ch.id
        for ch in session.execute(select(Channel).where(Channel.dataset_id == dataset_id)).scalars()
    ]
    total = 0
    for channel_id in channels:
        total += delete_values_for_channel(session, channel_id)
    return total
