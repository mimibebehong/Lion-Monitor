"""手动重算（S4A）：按**当前生效参数**重算某通道节点的历史点位，旧值就地覆盖。

为什么单独一个模块：重算的取值口径（哪些指标、哪些文件、时间戳怎么拼、跳过什么）
必须只有一份，页面只负责「按钮 + 确认提示 + 结果提示」。

口径（2026-11-02 定，见 ``docs/kanban/bodies/s4a_brief.md``）：

- **只做手动**（Q4）：参数/转速改了以后旧值 ``stale=1``，用户点「重算本节点」才重算；
- 复用导入链路：``pipeline.metric_jobs`` 解析该节点的生效参数与转速（缺转速 / 没有计算
  实现就跳过并记原因）→ ``metrics.compute_metric_series`` 逐窗算 → ``store.metrics`` 落库；
- 区间 = 该通道节点名下的**全部批次源文件**（跨数据集）；时间戳仍按
  ``采集起始 + 窗中心偏移``（``pipeline.moment_stamp``），与导入同一口径，所以同一
  ``(通道, 节点, 指标, computed_at)`` 能对上；
- 旧值处理取方案一（与导入覆盖口径一致）：同一时点**直接覆盖**为新值并置 ``stale=0``；
  窗长 / 步长改了以后不再产生的旧点位**软删**留痕（否则「待重算 N 条」永远清不到 0）；
- 导入时判无效的文件不参与重算（导入就没给它写过值，重算也不该凭空造出来）；
- 一次重算一条审计（``change_log``，entity_type=``metric_value``）+ 一条日志
  「重算 N 条」。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..constants import METRIC_UNITS
from ..ingest.vibration import VibrationFormatError, read_vibration
from ..store import audit, tree
from ..store import datasets as dataset_store
from ..store import metrics as metric_store
from ..store.metrics import NodeValue
from .metrics import compute_metric_series, short_sample_reason, window_plan
from .pipeline import MetricJob, SkippedMetric, metric_jobs, moment_stamp

__all__ = [
    "SECONDS_PER_FILE",
    "SECONDS_PER_POINT",
    "RecomputePlan",
    "RecomputeResult",
    "recompute_node",
    "recompute_plan",
]

logger = logging.getLogger(__name__)

#: 耗时估算系数：每文件读盘解析（秒）与每个计算点（秒）。
#: **经验值，跟机器性能绑定**（开发机实测校准，换机器/换磁盘会偏），只用于按钮旁的「预计」提示，
#: 不参与任何调度或超时判断；页面文案必须写明「估算」。
SECONDS_PER_FILE = 0.1
SECONDS_PER_POINT = 0.02


@dataclass(slots=True)
class RecomputePlan:
    """重算前的预览：算哪些指标、几个文件、几条点位、大概多久。"""

    node_id: int
    node_name: str
    object_name: str
    codes: list[str] = field(default_factory=list)
    stale_count: int = 0
    file_count: int = 0
    point_estimate: int = 0
    estimate_seconds: float = 0.0
    #: 该节点上不能重算的指标及原因（缺转速 / 没有计算实现）
    skipped: list[str] = field(default_factory=list)
    #: 无法重算的结构性问题（树上没指标 / 还没导入数据）
    problems: list[str] = field(default_factory=list)

    @property
    def can_run(self) -> bool:
        return bool(self.codes) and self.file_count > 0 and not self.problems


@dataclass(slots=True)
class RecomputeResult:
    """一次重算的结果：写了几条、作废几条、待重算数前后各是多少。"""

    node_id: int
    node_name: str
    codes: list[str] = field(default_factory=list)
    recomputed: int = 0
    retired: int = 0
    stale_before: int = 0
    stale_after: int = 0
    files: int = 0
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _channel_node(session: Session, node_id: int) -> tree.NodeRow:
    """取通道节点；重算只对通道节点做（指标节点由页面先解析成所属通道节点）。"""
    node = tree.get_node(session, node_id)
    if node.node_type != "channel":
        raise ValueError(f"重算只针对通道节点，节点 {node_id} 是 {node.type_text}")
    return node


def _object_name(session: Session, node: tree.NodeRow) -> str:
    """通道节点往上找监测对象名（找不到留空，不报错）。"""
    if node.parent_id is None:
        return ""
    parent = tree.get_node(session, node.parent_id)
    return parent.name if parent.node_type == "component" else ""


def _node_jobs(
    session: Session, task_id: int, node_id: int, code: str | None
) -> tuple[list[MetricJob], list[SkippedMetric]]:
    """该通道节点能算的指标任务 + 被跳过的指标（``code`` 给了就只留那个）。"""
    jobs, skipped = metric_jobs(session, task_id)
    picked = [job for job in jobs if job.node_id == node_id and (code is None or job.code == code)]
    dropped = [
        item for item in skipped if item.node_id == node_id and (code is None or item.code == code)
    ]
    return picked, dropped


def _estimate(
    jobs: list[MetricJob], files: list[dataset_store.ChannelFileRow]
) -> tuple[int, float]:
    """预计点位与耗时：按库里登记的点数 / 采样率先切一遍窗（不读文件）。"""
    points = 0
    for file in files:
        if file.sample_rate <= 0:
            continue
        for job in jobs:
            points += len(window_plan(file.points, file.sample_rate, job.params))
    seconds = len(files) * SECONDS_PER_FILE + points * SECONDS_PER_POINT
    return points, max(1.0, round(seconds, 1))


def recompute_plan(
    session: Session, task_id: int, node_id: int, code: str | None = None
) -> RecomputePlan:
    """重算预览（按钮旁的「将重算 N 条、预计耗时」就用它）。"""
    node = _channel_node(session, node_id)
    jobs, skipped = _node_jobs(session, task_id, node_id, code)
    files = dataset_store.channel_files(session, task_id, node.name)
    points, seconds = _estimate(jobs, files)
    plan = RecomputePlan(
        node_id=node.id,
        node_name=node.name,
        object_name=_object_name(session, node),
        codes=[job.code for job in jobs],
        stale_count=metric_store.count_stale(session, task_id, node_id=node_id, code=code),
        file_count=len(files),
        point_estimate=points,
        estimate_seconds=seconds,
        skipped=[f"{item.code}：{item.reason}" for item in skipped],
    )
    if not jobs:
        plan.problems.append("该通道节点没有可重算的指标（树上没挂指标，或指标被跳过）")
    elif not files:
        plan.problems.append("该通道节点还没有导入过数据（没有可回读的源文件）")
    return plan


def _stamp_points(file, job: MetricJob, points, sample_rate: float) -> list[tuple[str, NodeValue]]:
    """（偏移, 值）→ （时间戳, 待落库行）：时间戳与参数快照都按本次文件采样率现算。"""
    return [
        (
            moment_stamp(file.collected_at, offset),
            NodeValue(
                node_id=job.node_id,
                code=job.code,
                value=value,
                unit=METRIC_UNITS.get(job.code, ""),
                params_hash=job.params_hash_for(sample_rate),
                effective_params_json=job.effective_params_json(sample_rate),
                rpm_used=job.rpm,
                rpm_source=job.rpm_source,
            ),
        )
        for offset, value in points
    ]


def recompute_node(
    session: Session,
    task_id: int,
    node_id: int,
    code: str | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> RecomputeResult:
    """按当前生效参数重算该通道节点的历史点位（写库并提交，返回重算统计）。

    ``code`` 给了就只重算该指标（趋势页按「当前节点 + 指标」重算用）；不给 = 该节点
    树上挂的全部指标（评级页按节点重算用）。

    ``progress`` 可选：按「文件 × 指标」的完成步数回调 ``(fraction, text)``，
    页面用它喂 ``st.progress``；不传则行为与原来完全一致。
    """
    node = _channel_node(session, node_id)
    jobs, skipped = _node_jobs(session, task_id, node_id, code)
    result = RecomputeResult(
        node_id=node.id,
        node_name=node.name,
        codes=[job.code for job in jobs],
        stale_before=metric_store.count_stale(session, task_id, node_id=node_id, code=code),
        skipped=[f"{item.code}：{item.reason}" for item in skipped],
    )
    if not jobs:
        result.problems.append("该通道节点没有可重算的指标（树上没挂指标，或指标被跳过）")
        return result

    files = dataset_store.channel_files(session, task_id, node.name)
    if not files:
        result.problems.append("该通道节点还没有导入过数据（没有可回读的源文件）")
        return result

    total_steps = max(1, len(files) * len(jobs))
    done_steps = 0

    def _tick(text: str) -> None:
        nonlocal done_steps
        done_steps += 1
        if progress is not None:
            progress(min(done_steps / total_steps, 1.0), text)

    for file in files:
        result.files += 1
        if file.validity == "fail":
            result.skipped.append(f"{file.file_name}：导入时判无效，不重算")
            for _job in jobs:  # 无效文件不再算，但仍占步数，进度条不会卡在中途
                _tick(f"{file.file_name}：导入时判无效，跳过")
            continue
        if not file.collected_at:
            result.problems.append(f"{file.file_name}：批次缺少采集起始时间，无法定位点位")
            continue
        try:
            record = read_vibration(file.path)
        except (VibrationFormatError, OSError, ValueError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            result.problems.append(f"{file.file_name}：读不了原始文件（{detail}）")
            logger.warning("重算跳过文件 %s：%s", file.file_name, detail)
            continue

        sample_rate = float(record.meta.sample_rate_hz)
        for job in jobs:
            short_reason = short_sample_reason(record.samples.size, sample_rate, job.params)
            if short_reason:
                result.skipped.append(f"{file.file_name} · {job.code}：{short_reason}")
                _tick(f"{file.file_name} · {job.code}：{short_reason}")
                continue
            series = compute_metric_series(
                record.samples, sample_rate, params=job.params, codes=[job.code]
            )[job.code]
            points = _stamp_points(file, job, series, sample_rate)
            written, retired = metric_store.replace_node_series(
                session, file.channel_id, node_id, job.code, points
            )
            result.recomputed += written
            result.retired += retired
            _tick(f"{node.name} · {job.code}（{done_steps}/{total_steps}）")

    result.stale_after = metric_store.count_stale(session, task_id, node_id=node_id, code=code)
    audit.record_change(
        session,
        entity_type="metric_value",
        entity_id=node_id,
        field="recompute",
        old_value={"stale": result.stale_before, "recomputed": 0},
        new_value={
            "stale": result.stale_after,
            "recomputed": result.recomputed,
            "retired": result.retired,
        },
        actor=audit.actor_of(session, task_id),
        note=f"{node.name} 重算 {len(result.codes)} 个指标",
    )
    session.commit()
    logger.info(
        "重算完成：节点 %s·%s 覆盖 %d 条（作废 %d 条，文件 %d 个），待重算 %d → %d",
        node.name,
        " / ".join(result.codes),
        result.recomputed,
        result.retired,
        result.files,
        result.stale_before,
        result.stale_after,
    )
    return result
