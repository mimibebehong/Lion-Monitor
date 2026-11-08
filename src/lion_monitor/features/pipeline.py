"""导入管线：目录 → 数据集 → 逐通道节点指标计算 → 有效性判定。

口径（方案 A，2026-10-31 拍板）：**值按节点存、按参数指纹去重**。

- 每个通道号找出挂载它的**全部**通道节点（同一通道号可挂多个监测对象）
- 每个节点按「指标定义默认 + 节点覆盖 diff + params_locked 固化」解析该指标的生效参数
- 转速用 ``resolve_rpm``（节点手填 > 批次实测 > 对象额定）解析，拿不到就**不猜**
- 按 ``(channel_id, node_id, metric_code)`` 落库，同时写 ``params_hash``/``rpm_used``/``rpm_source``
- 同一通道 + 指标 + 参数指纹 + 转速 只真算一次，结果写多份节点行
  （``compute_runs`` / ``dedupe_hits`` 计数器留证据）
- 需要转速却没转速、或本期没有计算实现的指标**跳过并记录原因**（日志 + ``skipped_metrics``）
- 样本不足一窗（``n < window_s × fs``）的指标**不出值**，原因写 ``skipped_short`` 与日志，
  该文件因「不足一窗」判无效（G12，2026-11-02 现场定）
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..constants import METRIC_UNITS
from ..ingest.vibration import VibrationFormatError, VibrationRecord, read_vibration
from ..params import (
    PARAM_SCHEMA,
    default_params,
    effective_params_json,
    merge_params,
    needs_speed,
    params_hash,
    resolve_rpm,
)
from ..store import datasets, metric_assign, metric_def, object_registry, tree
from ..store.db import begin_immediate
from ..store.metrics import NodeValue
from ..store.metrics import upsert_node_values as _write_values
from . import metric_plugins
from .metrics import compute_metric_series, short_sample_reason

__all__ = [
    "BadFile",
    "ImportResult",
    "MetricJob",
    "PlanRow",
    "ShortSample",
    "SkippedMetric",
    "aggregate_plan",
    "import_batch",
    "metric_jobs",
    "metric_plan",
    "moment_stamp",
]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MetricJob:
    """一个计算任务：某个通道节点 × 某个指标 + 该节点解析出的生效参数与转速。

    ``params`` 是节点解析出的**未夹紧**生效参数：带通上限是否越过奈奎斯特要读到文件
    才知道（fs 是每个文件自己头的），所以指纹、去重键、落库快照都按文件采样率现算
    （``params_hash_for`` / ``dedupe_key_for`` / ``effective_params_json``），
    不在建任务时定死（S6）。
    """

    node_id: int
    node_name: str
    object_name: str
    channel_code: str
    code: str
    params: dict[str, Any] = field(default_factory=dict)
    rpm: float | None = None
    rpm_source: str = ""

    def params_hash_for(self, sample_rate: float | None) -> str:
        """本文件采样率下的参数指纹：指标码 + fs + 夹紧后的生效参数。"""
        return params_hash(self.params, code=self.code, sample_rate=sample_rate)

    def effective_params_json(self, sample_rate: float | None) -> str:
        """本文件采样率下的实际生效参数 JSON（随值落库，追溯用）。"""
        return effective_params_json(self.params, sample_rate)

    def dedupe_key_for(self, sample_rate: float | None) -> tuple[str, str, float | None]:
        """去重键：指标码 + 参数指纹（含 fs 与夹紧带通）+ 转速。"""
        return (self.code, self.params_hash_for(sample_rate), self.rpm)


@dataclass(slots=True)
class SkippedMetric:
    """被跳过的指标：说清是哪个节点、哪个指标、为什么不算（不静默吞掉）。"""

    node_id: int
    node_name: str
    object_name: str
    channel_code: str
    code: str
    reason: str


@dataclass(slots=True)
class BadFile:
    """一个没能入库的通道文件：文件名 + 通道号 + 原因。

    ``damaged`` 为真表示「读文件就失败」（截断/长度不符/头部损坏），
    为假表示读出来了但被判无效（例如信号近似为零）——两者在页面上口径不同。
    """

    filename: str
    channel_code: str
    reason: str
    damaged: bool = False


@dataclass(slots=True)
class ShortSample:
    """因「不足一窗」没出值的指标：节点 + 指标码 + 文件 + 具体原因（G12）。"""

    filename: str
    node_id: int
    node_name: str
    object_name: str
    channel_code: str
    code: str
    reason: str


@dataclass(slots=True)
class ImportResult:
    dataset_id: int | None
    name: str
    collected_at: str
    status: str
    channels: int
    files_ok: int
    files_bad: list[BadFile]
    metrics_written: int
    unmapped_channels: list[str]
    remark: str = ""
    #: 整批文件全部无法读取时，数据集不保留——此时 dataset_id 为 None
    #: （身体读不了 = 建了再软删留痕；头部就读不了 = 根本不建，见 G8）
    discarded: bool = False
    planned_metrics: dict[str, list[str]] | None = None
    channels_without_metrics: list[str] | None = None
    compute_runs: int = 0
    dedupe_hits: int = 0
    skipped_metrics: list[SkippedMetric] | None = None
    #: 因「样本不足一窗」不出值的指标明细（G12）：节点 / 指标码 / 文件 / 具体原因
    skipped_short: list[ShortSample] | None = None
    #: 健康树已装配、但本批一个文件都没有的通道号（按树顺序；只提示，不阻断导入）
    missing_channels: list[str] = field(default_factory=list)

    @property
    def files_bad_count(self) -> int:
        """没能入库的文件数（读失败 + 判无效）。"""
        return len(self.files_bad)

    @property
    def files_damaged(self) -> list[BadFile]:
        """其中「读文件就失败」的那部分（页面报「损坏 K 个」用）。"""
        return [item for item in self.files_bad if item.damaged]


def _judge_structure(points: int, sample_rate: float) -> tuple[bool, str]:
    """不算指标时的结构校验：只看点数与采样率。"""
    if points <= 0:
        return False, "采样点数为 0"
    if sample_rate <= 0:
        return False, "采样率不合法"
    return True, ""


def _judge(points: int, sample_rate: float, amplitude: float) -> tuple[bool, str]:
    if points <= 0:
        return False, "采样点数为 0"
    if sample_rate <= 0:
        return False, "采样率不合法"
    if amplitude <= 1e-9:
        return False, "信号近似为零（传感器未接或链路异常）"
    return True, ""


@dataclass(slots=True)
class PlanRow:
    """采购计划的一行：某个监测对象下的某个通道，将计算哪些指标。"""

    object_name: str
    channel_code: str
    channel_node_id: int
    metric_codes: list[str]


def metric_plan(session: Session, task_id: int) -> list[PlanRow]:
    """按「监测对象 × 通道」列出计算计划（同名通道挂多个对象会出多行）。"""
    nodes = tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in nodes}
    metrics_by_channel: dict[int, list[str]] = {}
    for node in nodes:
        if node.node_type != "metric" or node.parent_id is None:
            continue
        code = (node.attrs or {}).get("code") or node.name
        if not code:
            continue
        bucket = metrics_by_channel.setdefault(node.parent_id, [])
        if code not in bucket:
            bucket.append(code)

    rows: list[PlanRow] = []
    for node in nodes:
        if node.node_type != "channel":
            continue
        parent = by_id.get(node.parent_id)
        rows.append(
            PlanRow(
                object_name=parent.name if parent else "—",
                channel_code=node.name,
                channel_node_id=node.id,
                metric_codes=metrics_by_channel.get(node.id, []),
            )
        )
    return rows


def aggregate_plan(rows: list[PlanRow]) -> dict[str, list[str]]:
    """把同名通道的指标并起来（只看「要算哪些指标」，实际按节点各算各的）。"""
    merged: dict[str, list[str]] = {}
    for row in rows:
        bucket = merged.setdefault(row.channel_code, [])
        for code in row.metric_codes:
            if code not in bucket:
                bucket.append(code)
    return merged


def metrics_of_channel(session: Session, task_id: int, node_id: int) -> list[str]:
    """健康树里该通道节点下挂了哪些指标节点（按树顺序）。"""
    codes: list[str] = []
    for node in tree.list_nodes(session, task_id):
        if node.node_type != "metric" or node.parent_id != node_id:
            continue
        code = (node.attrs or {}).get("code") or node.name
        if code and code not in codes:
            codes.append(code)
    return codes


def planned_metrics(
    session: Session, task_id: int, channel_nodes: dict[str, int] | None = None
) -> dict[str, list[str]]:
    """通道号 → 将计算的指标（同名通道跨对象取并集）。"""
    return aggregate_plan(metric_plan(session, task_id))


def _rated_rpm_of(
    object_node: tree.NodeRow | None,
    objects_by_id: dict[int, object_registry.ObjectRow],
    objects_by_name: dict[str, object_registry.ObjectRow],
) -> float | None:
    """监测对象额定转速（转速三级里的 ``rated``）：优先按节点引用，老数据按名称匹配。"""
    if object_node is None:
        return None
    ref_id = object_node.ref_id if object_node.ref_type == "monitor_object" else None
    obj = objects_by_id.get(ref_id) if ref_id else objects_by_name.get(object_node.name)
    return obj.rated_rpm if obj is not None else None


def metric_jobs(session: Session, task_id: int) -> tuple[list[MetricJob], list[SkippedMetric]]:
    """解析计算任务：每个（通道节点 × 指标）一条，附生效参数、参数指纹与转速。

    跳过的指标单独返回（缺转速 / 本期没有计算实现），调用方负责展示与记日志。
    """
    nodes = tree.list_nodes(session, task_id)
    by_id = {node.id: node for node in nodes}
    definitions = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    assignments = {
        (row.node_id, row.code): row
        for row in metric_assign.list_assignments(session, task_id)
        if row.enabled
    }

    codes_by_channel: dict[int, list[str]] = {}
    metric_node_of: dict[tuple[int, str], int] = {}
    for node in nodes:
        if node.node_type != "metric" or node.parent_id is None:
            continue
        code = (node.attrs or {}).get("code") or node.name
        if not code:
            continue
        bucket = codes_by_channel.setdefault(node.parent_id, [])
        if code not in bucket:
            bucket.append(code)
        metric_node_of.setdefault((node.parent_id, code), node.id)

    jobs: list[MetricJob] = []
    skipped: list[SkippedMetric] = []
    objects_by_id = {row.id: row for row in object_registry.list_objects(session, task_id)}
    objects_by_name = {row.name: row for row in objects_by_id.values()}
    for channel in (node for node in nodes if node.node_type == "channel"):
        object_node = by_id.get(channel.parent_id) if channel.parent_id else None
        object_name = object_node.name if object_node else "—"
        rated_rpm = _rated_rpm_of(object_node, objects_by_id, objects_by_name)
        rpm, rpm_source = resolve_rpm(
            node_rpm=tree.node_rpm(nodes, channel.id), rated_rpm=rated_rpm
        )
        for code in codes_by_channel.get(channel.id, []):
            definition = definitions.get(code)
            if definition is not None and not definition.enabled:
                skipped.append(
                    SkippedMetric(
                        node_id=metric_node_of.get((channel.id, code), channel.id),
                        node_name=code,
                        object_name=object_name,
                        channel_code=channel.name,
                        code=code,
                        reason=f"{code}：指标定义已停用（在「指标配置」页启用后才计算）",
                    )
                )
                continue
            if definition is None and code not in PARAM_SCHEMA:
                skipped.append(
                    SkippedMetric(
                        node_id=metric_node_of.get((channel.id, code), channel.id),
                        node_name=code,
                        object_name=object_name,
                        channel_code=channel.name,
                        code=code,
                        reason=f"{code}：指标定义不存在（可能已删除）",
                    )
                )
                continue
            schema = definition.schema if definition else PARAM_SCHEMA.get(code, [])
            assigned = assignments.get((metric_node_of.get((channel.id, code)), code))
            if assigned is not None:
                params = metric_assign.compose_params(
                    code,
                    definition.defaults if definition else {},
                    assigned.params,
                    assigned.params_locked,
                )
            else:
                params = merge_params(
                    default_params(code), definition.defaults if definition else {}
                )
            reason = _skip_reason(code, schema, rpm)
            if reason:
                skipped.append(
                    SkippedMetric(
                        node_id=channel.id,
                        node_name=channel.name,
                        object_name=object_name,
                        channel_code=channel.name,
                        code=code,
                        reason=reason,
                    )
                )
                continue
            jobs.append(
                MetricJob(
                    node_id=channel.id,
                    node_name=channel.name,
                    object_name=object_name,
                    channel_code=channel.name,
                    code=code,
                    params=params,
                    rpm=rpm,
                    rpm_source=rpm_source or "",
                )
            )
    return jobs, skipped


def _skip_reason(code: str, schema: list[dict[str, Any]], rpm: float | None) -> str:
    """不能算的原因（空串 = 可以算）；先看转速门控，再看有没有计算实现。"""
    if needs_speed(schema) and rpm is None:
        return "缺少转速：该指标含按转速缩放的特征频率项，节点手填 / 批次实测 / 对象额定都没取到"
    if code not in METRIC_UNITS and code not in metric_plugins.load_plugins()[0]:
        implemented = " / ".join(METRIC_UNITS)
        return (
            f"没有计算实现：{code} 既不是内置指标（{implemented}），"
            f"插件目录 {metric_plugins.plugin_dir()} 里也没有这个码"
        )
    return ""


def _amplitude_of(cache: dict[tuple[str, str, float | None], list[tuple[float, float]]]) -> float:
    """判有效性用的幅值：取本通道已算指标里最大的绝对值（多窗序列取最大点）。

    原来只认 a_RMS——通道没配 a_RMS 时恒为 0，正常文件会被误判「信号近似为零」
    （P1 收口：判无效的通道不再写值，若判级还只看 a_RMS 就会把好数据丢掉）。
    """
    return max(
        (abs(value) for points in cache.values() for _offset, value in points),
        default=0.0,
    )


def import_batch(
    session: Session,
    task_id: int,
    directory: str | Path,
    channel_nodes: dict[str, int],
    name: str | None = None,
    compute: bool = True,
    progress: Callable[[float, str], None] | None = None,
) -> ImportResult:
    """导入一个采集目录；``compute`` 为真时按健康树里各通道节点挂的指标计算。

    ``channel_nodes`` 是 通道号 → 健康树通道节点 id（保留兼容；实际按树里全部同名通道节点算）。

    **一次导入 = 一个事务**（现场拍板 P1 / B2）：建数据集 + 建通道 + 写指标值 + 落状态
    全在同一个 SAVEPOINT 里，中途抛异常（计算报错 / 写库冲突）**整体回滚**——库内
    0 行新增，不留「待判」数据集、不留半批通道或指标值；成功时一次提交。

    单个文件读不出来（截断/长度不符/头部损坏）只降级「该文件无效」并记进
    ``files_bad``，其余文件照常入库；整批一个有效文件都没有时不保留数据集
    （软删留痕，不留「待判 + 0 值」空壳）。

    批次状态三态（2026-10-31 拍板 Q3）：全部成功 = ``有效``；有成功也有失败 =
    ``部分有效``（备注与 ``files_bad`` 点名哪个通道失败、原因）；全部失败 = ``无效``。
    指标值只写「判定有效」的通道，判无效的通道（信号近似为零等）不写值，避免 0 值
    混进趋势 / 评级。

    并发（复查 v2 严重项 2）：开头先 ``BEGIN IMMEDIATE`` 取写锁再开 SAVEPOINT——
    ``SAVEPOINT`` 起头的 DEFERRED 事务「先读后写」升级时撞别的写者会直接
    ``database is locked``，``busy_timeout`` 不生效；先取锁则第二个导入在入口排队。
    """
    begin_immediate(session)
    savepoint = session.begin_nested()
    try:
        result = _import_batch_inner(
            session, task_id, directory, channel_nodes, name, compute, progress
        )
    except BaseException:
        # 批次事务整体回滚：库内 0 行新增；异常照旧抛给页面（页面只给一条中文提示）
        savepoint.rollback()
        raise
    savepoint.commit()
    session.commit()
    return result


def _bad_header_reason(item: datasets.BadSourceFile) -> str:
    """坏头文件的原因文案：去掉振动层已经带过的文件名前缀，页面不重复文件名。"""
    text = item.reason
    for prefix in (f"{item.filename}: ", f"{item.filename}："):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return f"头部读不了：{text}"


_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def moment_stamp(collected_at: str, offset: float) -> str:
    """记录采集起始时间 + 窗中心偏移 → 落库时间戳。

    整秒偏移沿用 ``%Y-%m-%d %H:%M:%S``（与历史行一致）；偏移带小数时保留微秒，
    否则秒以下步长（window/step < 1 s）的相邻点会落进同一秒、互相覆盖。
    """
    try:
        start = datetime.strptime(collected_at, _TIME_FMT)
    except ValueError:
        return collected_at
    moment = start + timedelta(seconds=float(offset))
    if moment.microsecond:
        return moment.strftime("%Y-%m-%d %H:%M:%S.%f")
    return moment.strftime(_TIME_FMT)


def _write_series(
    session: Session,
    channel_id: int,
    collected_at: str,
    records: list[tuple[float, NodeValue]],
) -> int:
    """把一条通道的（时间偏移, 值）按时间点分批落库：同一时点的所有指标行一起写。"""
    grouped: dict[str, list[NodeValue]] = defaultdict(list)
    for offset, record in records:
        grouped[moment_stamp(collected_at, offset)].append(record)
    return sum(_write_values(session, channel_id, stamp, items) for stamp, items in grouped.items())


def _header_unreadable_result(
    directory: str | Path, problems: list[datasets.BadSourceFile]
) -> ImportResult:
    """整批文件头部都读不了：不建数据集，返回「没留下数据集」的结果（复查 G8）。

    与 S-2「整批文件都读不出来」同一口径：不抛异常、``discarded=True``、逐文件点名原因，
    页面按已有文案给一条中文提示。区别只是这里一个数据集行都没建（无需软删留痕）。
    """
    bad = [
        BadFile(
            filename=item.filename,
            channel_code="",
            reason=_bad_header_reason(item),
            damaged=True,
        )
        for item in problems
    ]
    causes = list(dict.fromkeys(item.reason for item in bad))
    if len(causes) == 1:
        note = f"损坏 {len(bad)} 个文件：{'、'.join(item.filename for item in bad)}（{causes[0]}）"
    else:
        detail = "；".join(f"{item.filename}（{item.reason}）" for item in bad)
        note = f"损坏 {len(bad)} 个文件：{detail}"
    folder = Path(directory)
    return ImportResult(
        dataset_id=None,
        name=folder.name or str(folder),
        collected_at="",
        status="无效",
        channels=0,
        files_ok=0,
        files_bad=bad,
        metrics_written=0,
        unmapped_channels=[],
        remark=f"目录里的文件都读不了：{note}",
        discarded=True,
    )


def _import_batch_inner(
    session: Session,
    task_id: int,
    directory: str | Path,
    channel_nodes: dict[str, int],
    name: str | None,
    compute: bool,
    progress: Callable[[float, str], None] | None = None,
) -> ImportResult:
    """批次体：在调用方开好的事务里建数据集、算指标、落状态（不自己提交）。

    ``progress(fraction, text)`` 在逐文件读取/计算时被调用，供页面画进度条；
    不传就是纯后台导入。
    """

    def report(fraction: float, text: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), text)

    known_ids = {item.id for item in datasets.list_datasets(session, task_id)}
    row, header_problems = datasets.import_from_dir_detail(
        session, task_id, directory, name, commit=False
    )
    if row is None:
        # 整批文件头部都读不了（复查 G8）：建不出数据集，不抛异常，
        # 按「整批没留下数据集」返回，页面给一条中文提示（discarded=True）
        logger.warning("批次 %s 的文件头部全部读不了，不建数据集", directory)
        unreadable = _header_unreadable_result(directory, header_problems)
        report(1.0, "没有可导入的有效文件")
        logger.info(
            "导入结束：批次 %s → 无效（文件头部全部读不了，未建数据集），头部坏 %d 个",
            unreadable.name,
            unreadable.files_bad_count,
        )
        return unreadable
    is_new_batch = row.id not in known_ids
    files = datasets.list_files(session, row.id)
    # G15：导入开始（批次、文件数、坏头数、通道数、是否算指标）——落盘日志留痕
    logger.info(
        "导入开始：批次 %s（任务 %s），入库文件 %d 个 / 头部坏 %d 个，通道 %d 个，计算指标%s",
        row.name,
        task_id,
        len(files),
        len(header_problems),
        len({f.channel_code for f in files}),
        "开" if compute else "关",
    )
    plan = planned_metrics(session, task_id, channel_nodes)  # 通道号 → 指标（同名通道已取并集）
    # G2：树上已装配的通道号（按树顺序去重）− 本批有文件的通道号 = 漏采提示。
    # 只提示不阻断：不影响 files_bad（那是「文件进来了但坏了」），也不改批次三态。
    assembled = list(
        dict.fromkeys(
            node.name for node in tree.list_nodes(session, task_id) if node.node_type == "channel"
        )
    )
    present = {f.channel_code for f in files}
    missing = [code for code in assembled if code not in present]
    jobs, skipped = metric_jobs(session, task_id) if compute else ([], [])
    jobs_by_channel: dict[str, list[MetricJob]] = defaultdict(list)
    for job in jobs:
        jobs_by_channel[job.channel_code].append(job)
    for item in skipped:
        logger.warning(
            "跳过指标 %s @ %s·%s（节点 %s）：%s",
            item.code,
            item.object_name,
            item.channel_code,
            item.node_id,
            item.reason,
        )

    ok_count = 0
    # 本批「文件总数」= 进了库的通道文件 + 头部就读不了的文件（后者也要体现在状态里）
    total_files = len(files) + len(header_problems)
    # 头部就读不了的文件（时间/版本/采样率/scale/通道号）先记成坏文件：它们没进库，
    # 但必须在结果里点名，且让整批状态体现出来（复查 B3 / G8）
    bad: list[BadFile] = [
        BadFile(
            filename=item.filename,
            channel_code="",
            reason=_bad_header_reason(item),
            damaged=True,
        )
        for item in header_problems
    ]
    report(0.0, f"准备导入 {row.name}（{len(files)} 个文件）")
    written = 0
    compute_runs = 0
    dedupe_hits = 0
    unmapped: list[str] = []
    no_metrics: list[str] = []
    short_skips: list[ShortSample] = []

    for file_index, f in enumerate(files, start=1):
        report(
            (file_index - 1) / total_files,
            f"读取 {f.channel_code}（{file_index}/{len(files)}）",
        )
        channel_jobs = jobs_by_channel.get(f.channel_code, []) if compute else []
        codes = plan.get(f.channel_code, [])
        # 每个指标一条（时间偏移 → 值）序列：按 window_s / step_s 逐窗多点（S4W-1/S4W-2）
        records: list[tuple[float, NodeValue]] = []
        cache: dict[tuple[str, str, float | None], list[tuple[float, float]]] = {}
        file_short: list[str] = []

        broken = ""
        record: VibrationRecord | None = None
        if channel_jobs:
            try:
                record = read_vibration(f.path)
            except (VibrationFormatError, OSError, ValueError) as exc:
                # 单文件读失败只降级该文件：记进 files_bad 后继续处理其余文件（S-2）
                broken = f"文件损坏：{str(exc).strip() or type(exc).__name__}"
                reason = broken.removeprefix("文件损坏：").removeprefix(f"{f.file_name}: ")
                logger.warning("跳过损坏文件 %s：%s", f.file_name, reason)
        if record is not None:
            sample_rate = float(record.meta.sample_rate_hz)
            for job in channel_jobs:
                # G12：不足一窗 → 该指标不出值（2026-11-02 定），原因写进明细与日志
                short_reason = short_sample_reason(record.samples.size, sample_rate, job.params)
                if short_reason:
                    short_skips.append(
                        ShortSample(
                            filename=f.file_name,
                            node_id=job.node_id,
                            node_name=job.node_name,
                            object_name=job.object_name,
                            channel_code=job.channel_code,
                            code=job.code,
                            reason=short_reason,
                        )
                    )
                    file_short.append(short_reason)
                    logger.warning(
                        "样本不足一窗，跳过指标 %s @ %s·%s（节点 %s，文件 %s）：%s",
                        job.code,
                        job.object_name,
                        job.channel_code,
                        job.node_id,
                        f.file_name,
                        short_reason,
                    )
                    continue
                key = job.dedupe_key_for(sample_rate)
                if key in cache:
                    dedupe_hits += 1
                    points = cache[key]
                else:
                    points = compute_metric_series(
                        record.samples,
                        record.meta.sample_rate_hz,
                        params=job.params,
                        codes=[job.code],
                    )[job.code]
                    cache[key] = points
                    compute_runs += 1
                for offset, value in points:
                    records.append(
                        (
                            offset,
                            NodeValue(
                                node_id=job.node_id,
                                code=job.code,
                                value=value,
                                unit=metric_plugins.unit_of(job.code),
                                params_hash=key[1],
                                effective_params_json=job.effective_params_json(sample_rate),
                                rpm_used=job.rpm,
                                rpm_source=job.rpm_source,
                            ),
                        )
                    )
            if any(
                not math.isfinite(value) for points in cache.values() for _offset, value in points
            ):
                # 算出来的值非有限：该文件整体降级，绝不落库（NaN 会以 NULL 写库 → IntegrityError）
                broken = "计算结果非有限（NaN/Inf），已跳过该文件"
                records = []
                logger.warning("跳过 %s：%s", f.file_name, broken)

        ok = False
        if broken:
            datasets.set_file_status(session, f.id, "无效", broken)
            bad.append(
                BadFile(
                    filename=f.file_name,
                    channel_code=f.channel_code,
                    reason=broken,
                    damaged=True,
                )
            )
        else:
            if compute:
                if channel_jobs and file_short and not records:
                    # G12：本文件的指标全部因「不足一窗」没出值 → 判无效，原因写具体点数/窗长
                    ok, note = False, "；".join(dict.fromkeys(file_short))
                else:
                    ok, note = _judge(f.points, f.sample_rate, _amplitude_of(cache))
                if not codes:
                    ok, note = True, ""
                    no_metrics.append(f.channel_code)
                elif not channel_jobs:
                    ok, note = True, ""  # 指标全被跳过（缺转速等）：结构没问题，只是没值
            else:
                ok, note = _judge_structure(f.points, f.sample_rate)

            datasets.set_file_status(session, f.id, "有效" if ok else "无效", note)
            if ok:
                ok_count += 1
            else:
                bad.append(BadFile(filename=f.file_name, channel_code=f.channel_code, reason=note))

        if f.channel_code in plan and ok and records:
            # 值只对判定有效的通道落库：判无效的通道（信号近似为零等）不写值，
            # 否则 0 值会进趋势并参与评级（复查 S5 的误导点）
            written += _write_series(session, f.id, row.collected_at, records)
        elif f.channel_code not in plan:
            unmapped.append(f.channel_code)

        report(file_index / total_files, f"{f.channel_code} 已处理（{file_index}/{total_files}）")

    report(1.0, f"{row.name} 处理完成（{ok_count}/{total_files} 个文件有效）")

    # 批次三态（2026-10-31 拍板 Q3）：全部成功 = 有效；有成功也有失败 = 部分有效；
    # 全部失败 = 无效。「部分有效」不再折叠成「无效」（S5：用户看到「无效」会以为整批不能用）
    if total_files and ok_count == total_files:
        status = "有效"
    elif ok_count == 0:
        status = "无效"
    else:
        status = "部分有效"
    damaged = [item for item in bad if item.damaged]
    notes: list[str] = []
    if damaged:
        # 点名「哪个文件/通道坏了 + 原因」：一条原因时合并写，多条时逐条写（P1 / S5）
        names = "、".join(item.filename for item in damaged)
        causes = list(dict.fromkeys(item.reason for item in damaged))
        if len(causes) == 1:
            notes.append(f"损坏 {len(damaged)} 个文件：{names}（{causes[0]}）")
        else:
            detail = "；".join(f"{item.filename}（{item.reason}）" for item in damaged)
            notes.append(f"损坏 {len(damaged)} 个文件：{detail}")
    notes.extend(f"{item.channel_code}（{item.reason}）" for item in bad if not item.damaged)
    if total_files and len(damaged) == total_files and not is_new_batch:
        notes.append("整批文件都读不出来：数据集与历史指标值保留，未做改动")
    note = "" if status == "有效" else "；".join(notes)

    channels = len({f.channel_code for f in files})
    if total_files and len(damaged) == total_files and is_new_batch:
        # 整批文件一个都读不出来（截断/长度不符）：不留「待判 + 0 值」空壳
        # （软删留痕，回收站可查）。只是判无效（信号为零等）的批次照旧保留，行为不变。
        datasets.delete_dataset(session, row.id, operator="import_batch")
        logger.warning(
            "批次 %s 的 %d 个文件全部无法读取，不保留数据集：%s", row.name, len(files), note
        )
        logger.info(
            "导入结束：批次 %s → 无效（整批文件都读不了，不保留数据集）；文件成功 %d / 坏 %d，"
            "指标值 %d；缺通道 %s",
            row.name,
            ok_count,
            len(bad),
            written,
            "、".join(missing) or "无",
        )
        return ImportResult(
            dataset_id=None,
            name=row.name,
            collected_at=row.collected_at,
            status="无效",
            channels=channels,
            files_ok=ok_count,
            files_bad=bad,
            metrics_written=written,
            unmapped_channels=unmapped,
            remark=note,
            discarded=True,
            planned_metrics=plan,
            channels_without_metrics=no_metrics,
            compute_runs=compute_runs,
            dedupe_hits=dedupe_hits,
            skipped_metrics=skipped,
            skipped_short=short_skips,
            missing_channels=missing,
        )

    updated = datasets.update_dataset(session, row.id, status=status, remark=note)
    # G15：导入结束（状态、文件成败数、指标值、缺通道）——与开始那条成对，便于回溯
    logger.info(
        "导入结束：批次 %s → %s；文件成功 %d / 坏 %d，指标值 %d（真算 %d 次 / 复用 %d 次）；"
        "缺通道 %s",
        updated.name,
        status,
        ok_count,
        len(bad),
        written,
        compute_runs,
        dedupe_hits,
        "、".join(missing) or "无",
    )

    return ImportResult(
        dataset_id=updated.id,
        name=updated.name,
        collected_at=updated.collected_at,
        status=updated.status,
        channels=channels,
        files_ok=ok_count,
        files_bad=bad,
        metrics_written=written,
        unmapped_channels=unmapped,
        remark=note,
        planned_metrics=plan,
        channels_without_metrics=no_metrics,
        compute_runs=compute_runs,
        dedupe_hits=dedupe_hits,
        skipped_metrics=skipped,
        skipped_short=short_skips,
        missing_channels=missing,
    )
