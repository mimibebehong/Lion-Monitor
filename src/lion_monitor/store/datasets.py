"""数据集台账：一次采集 = 一个 Dataset（含 4 个 Channel 文件）。

沿用脚手架阶段定义的 ``dataset`` / ``channel`` 两张表：

- ``Dataset``：批次级信息（目录、通道数、起始时间、有效性）
- ``Channel``：单个通道文件（路径、采样率、点数、有效性）

对外暴露的行对象把内部字段换成业务口径（status↔validity、collected_at↔started_at），
让页面不必关心底层列名。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..ingest.vibration import scan_vibration_dir_checked
from .db import begin_immediate
from .models import Channel, Dataset, DeletionLog, MetricValue

__all__ = [
    "DATASET_STATUS",
    "BadSourceFile",
    "DatasetError",
    "DatasetRow",
    "ChannelFileRow",
    "FileRow",
    "add_file",
    "channel_files",
    "channel_sample_rate",
    "count_datasets_for_channel",
    "create_dataset",
    "delete_dataset",
    "get_dataset",
    "import_from_dir",
    "import_from_dir_detail",
    "list_datasets",
    "list_files",
    "set_measured_rpm",
    "update_dataset",
]

#: 数据集有效性状态（对应库里的 validity）；三态由马哥 2026-10-31 拍板（Q3）
DATASET_STATUS = ("待判", "有效", "部分有效", "无效")

_STATUS_TO_DB = {"待判": "pending", "有效": "pass", "部分有效": "partial", "无效": "fail"}
_STATUS_FROM_DB = {v: k for k, v in _STATUS_TO_DB.items()}

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class DatasetError(Exception):
    """数据集台账操作失败。"""


@dataclass(slots=True)
class DatasetRow:
    id: int
    task_id: int
    code: str
    name: str
    source_dir: str
    collected_at: str
    status: str
    remark: str
    file_count: int = 0
    channel_count: int = 0
    created_at: datetime | None = None


@dataclass(slots=True)
class FileRow:
    id: int
    dataset_id: int
    file_name: str
    path: str
    channel_code: str
    sample_rate: float
    points: int
    size_bytes: int
    status: str = "待判"


@dataclass(slots=True)
class ChannelFileRow:
    """任务下某个通道号的一个源文件（跨批次）。

    重算要按它回读原始样本、并把「采集起始时间」重新拼成点位时间戳（S4A），
    所以比 :class:`FileRow` 多带 ``collected_at`` / ``points`` / ``sample_rate``。
    """

    channel_id: int
    dataset_id: int
    file_name: str
    path: str
    channel_code: str
    collected_at: str
    sample_rate: float
    points: int
    validity: str = ""


def _alive(model) -> bool:
    return model is not None and model.deleted_at is None


def _fmt(moment: datetime | None) -> str:
    return moment.strftime(_TIME_FMT) if moment else ""


def _to_row(model: Dataset, file_count: int = 0, channel_count: int = 0) -> DatasetRow:
    return DatasetRow(
        id=model.id,
        task_id=model.task_id,
        code=model.code,
        name=model.name,
        source_dir=model.source_dir,
        collected_at=_fmt(model.started_at),
        status=_STATUS_FROM_DB.get(model.validity, "待判"),
        remark=model.validity_note or "",
        file_count=file_count,
        channel_count=channel_count or model.channel_count,
        created_at=model.imported_at,
    )


def _to_file(model: Channel) -> FileRow:
    path = Path(model.source_path)
    return FileRow(
        id=model.id,
        dataset_id=model.dataset_id,
        file_name=path.name,
        path=model.source_path,
        channel_code=model.code,
        sample_rate=float(model.sample_rate_hz),
        points=int(model.sample_count),
        size_bytes=path.stat().st_size if path.exists() else 0,
        status=_STATUS_FROM_DB.get(model.validity, "待判"),
    )


#: 编号分配冲突时的重试次数（多 tab 同时导入会抢同一个号）
_CODE_ATTEMPTS = 5


def _next_code(session: Session) -> str:
    """当天下一个数据集编号：**全局**最大号 + 1（不按任务计数）。

    ``dataset.code`` 是全局 UNIQUE，按「本任务条数 + 1」生成会在同一天第二个任务、
    或并发导入时撞号（复查 B1）。号段里的软删数据集照样占号，所以这里不过滤 ``deleted_at``。
    """
    prefix = f"DS-{datetime.now():%Y%m%d}-"
    highest = 0
    stmt = select(Dataset.code).where(Dataset.code.like(f"{prefix}%"))
    for code in session.execute(stmt).scalars():
        suffix = code[len(prefix) :]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:03d}"


def create_dataset(
    session: Session,
    task_id: int,
    name: str,
    source_dir: str,
    collected_at: str,
    status: str = "待判",
) -> DatasetRow:
    clean = (name or "").strip()
    if not clean:
        raise DatasetError("数据集名称不能为空")
    if status not in DATASET_STATUS:
        raise DatasetError(f"未知状态：{status}（可选 {'/'.join(DATASET_STATUS)}）")
    started = None
    for fmt in (_TIME_FMT, "%Y-%m-%d %H:%M", "%Y%m%d%H%M%S"):
        try:
            started = datetime.strptime(collected_at, fmt)
            break
        except ValueError:
            continue

    # 编号全局单调 + 撞号重试：并发导入时两个写者可能读到同一个 max，
    # 后写的那个吃 IntegrityError → 回到 SAVEPOINT 重新取号再写（复查 B1）。
    # 这里只 flush 不 commit：事务边界由调用方（批次事务）持有，撞号也只用
    # SAVEPOINT 局部回滚，不会把同一批次里已经写好的行一起丢掉（P1 / B2）。
    # 先取写锁：本函数是「读 max → 写」的写路径，DEFERRED 事务升级会直接
    # database is locked（复查 v2 严重项 2）。
    begin_immediate(session)
    for attempt in range(_CODE_ATTEMPTS):
        model = Dataset(
            task_id=task_id,
            code=_next_code(session),
            name=clean,
            source_dir=norm_dir(source_dir),
            channel_count=0,
            started_at=started,
            validity=_STATUS_TO_DB[status],
            validity_note="",
            imported_at=datetime.now(),
        )
        try:
            with session.begin_nested():
                session.add(model)
        except IntegrityError:
            if attempt == _CODE_ATTEMPTS - 1:
                raise DatasetError(
                    "数据集编号分配失败：同时有多个导入在跑，请稍后重试该批次"
                ) from None
            continue
        return _to_row(model)
    raise DatasetError("数据集编号分配失败：请稍后重试该批次")  # pragma: no cover - 循环已返回/抛出


def add_file(
    session: Session,
    dataset_id: int,
    path: str | Path,
    channel_code: str,
    sample_rate: float,
    points: int,
) -> FileRow:
    """登记一个通道文件；只 flush 不 commit（事务由批次导入方持有）。"""
    p = Path(path)
    model = Channel(
        dataset_id=dataset_id,
        code=str(channel_code),
        source_path=str(p),
        sample_rate_hz=float(sample_rate),
        sample_count=int(points),
        duration_s=float(points) / float(sample_rate) if sample_rate else 0.0,
        validity="pending",
        validity_note="",
    )
    session.add(model)
    session.flush()

    dataset = session.get(Dataset, dataset_id)
    if dataset is not None:
        alive = len(
            list(
                session.execute(
                    select(Channel.id).where(
                        Channel.dataset_id == dataset_id, Channel.deleted_at.is_(None)
                    )
                ).scalars()
            )
        )
        dataset.channel_count = alive
        session.flush()
    return _to_file(model)


def norm_dir(path: str | Path) -> str:
    """目录归一化：去掉结尾斜杠，便于比对同一批次。"""
    return str(Path(os.fspath(path)).expanduser().resolve())


def processed_dirs(session: Session, task_id: int) -> dict[str, str]:
    """已处理目录索引：源目录 → 数据集编号（软删的批次不算）。"""
    stmt = select(Dataset).where(Dataset.task_id == task_id, Dataset.deleted_at.is_(None))
    index: dict[str, str] = {}
    for model in session.execute(stmt).scalars():
        if not model.source_dir:
            continue
        index[norm_dir(model.source_dir)] = model.code
    return index


def list_datasets(session: Session, task_id: int) -> list[DatasetRow]:
    stmt = (
        select(Dataset)
        .where(Dataset.task_id == task_id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.started_at, Dataset.id)
    )
    rows: list[DatasetRow] = []
    for model in session.execute(stmt).scalars():
        files = list_files(session, model.id)
        rows.append(_to_row(model, len(files), len({f.channel_code for f in files})))
    return rows


def get_dataset(session: Session, dataset_id: int) -> DatasetRow:
    model = session.get(Dataset, dataset_id)
    if not _alive(model):
        raise DatasetError(f"数据集不存在：{dataset_id}")
    files = list_files(session, dataset_id)
    return _to_row(model, len(files), len({f.channel_code for f in files}))


def channel_sample_rate(session: Session, task_id: int, channel_code: str) -> float | None:
    """该通道号在任务下的采样率（取最小值，校验带通上限用）；没导入过数据返回 None。"""
    stmt = (
        select(Channel.sample_rate_hz)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Dataset.deleted_at.is_(None),
            Channel.code == channel_code,
            Channel.deleted_at.is_(None),
        )
    )
    rates = [float(rate) for rate in session.execute(stmt).scalars() if rate and rate > 0]
    return min(rates) if rates else None


def count_datasets_for_channel(session: Session, task_id: int, channel_code: str) -> int:
    """任务下含该通道号的有效批次数量（改带宽参数时提示「将影响 N 个数据集」）。"""
    stmt = (
        select(Dataset.id)
        .join(Channel, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Dataset.deleted_at.is_(None),
            Channel.code == channel_code,
            Channel.deleted_at.is_(None),
        )
        .distinct()
    )
    return len(list(session.execute(stmt).scalars()))


def list_files(session: Session, dataset_id: int) -> list[FileRow]:
    stmt = (
        select(Channel)
        .where(Channel.dataset_id == dataset_id, Channel.deleted_at.is_(None))
        .order_by(Channel.code, Channel.id)
    )
    return [_to_file(model) for model in session.execute(stmt).scalars()]


def channel_files(session: Session, task_id: int, channel_code: str) -> list[ChannelFileRow]:
    """任务下该通道号的**全部源文件**（跨批次，按采集时间升序）——重算回读样本用。

    只取未删的数据集与未删的通道行；采集时间取批次起始时间（重算时点位时间戳
    = 起始时间 + 窗中心偏移，与导入同一口径）。
    """
    stmt = (
        select(Channel, Dataset)
        .join(Dataset, Channel.dataset_id == Dataset.id)
        .where(
            Dataset.task_id == task_id,
            Dataset.deleted_at.is_(None),
            Channel.code == channel_code,
            Channel.deleted_at.is_(None),
        )
        .order_by(Dataset.started_at, Channel.id)
    )
    rows: list[ChannelFileRow] = []
    for channel, dataset in session.execute(stmt):
        path = Path(channel.source_path)
        rows.append(
            ChannelFileRow(
                channel_id=channel.id,
                dataset_id=dataset.id,
                file_name=path.name,
                path=channel.source_path,
                channel_code=channel.code,
                collected_at=_fmt(dataset.started_at),
                sample_rate=float(channel.sample_rate_hz),
                points=int(channel.sample_count),
                validity=channel.validity or "",
            )
        )
    return rows


def update_dataset(
    session: Session,
    dataset_id: int,
    name: str | None = None,
    status: str | None = None,
    remark: str | None = None,
) -> DatasetRow:
    begin_immediate(session)  # 先读后写：先取写锁，免得并发时事务升级失败
    model = session.get(Dataset, dataset_id)
    if not _alive(model):
        raise DatasetError(f"数据集不存在：{dataset_id}")
    if name is not None:
        clean = name.strip()
        if not clean:
            raise DatasetError("数据集名称不能为空")
        model.name = clean
    if status is not None:
        if status not in DATASET_STATUS:
            raise DatasetError(f"未知状态：{status}（可选 {'/'.join(DATASET_STATUS)}）")
        model.validity = _STATUS_TO_DB[status]
    if remark is not None:
        model.validity_note = remark
    session.flush()
    return get_dataset(session, dataset_id)


def set_file_status(session: Session, channel_id: int, status: str, note: str = "") -> None:
    """单独给某个通道文件打有效性（幅值异常等只影响单通道）；只 flush 不 commit。"""
    if status not in DATASET_STATUS:
        raise DatasetError(f"未知状态：{status}")
    begin_immediate(session)  # 先读后写：先取写锁，免得并发时事务升级失败
    model = session.get(Channel, channel_id)
    if not _alive(model):
        raise DatasetError(f"通道文件不存在：{channel_id}")
    model.validity = _STATUS_TO_DB[status]
    model.validity_note = note
    session.flush()


def set_measured_rpm(session: Session, dataset_id: int, rpm: float | None) -> None:
    """写批次实测转速（转速三级里的 ``measured``）；``None`` 视为清空。

    实测转速按批次存（同一批 4 个通道共用一个转速），供 CF-1 的频率派生使用。
    """
    begin_immediate(session)  # 先读后写：先取写锁，免得并发时事务升级失败
    model = session.get(Dataset, dataset_id)
    if model is None or model.deleted_at is not None:
        raise DatasetError(f"数据集不存在：{dataset_id}")
    if rpm is None:
        model.measured_rpm = None
    else:
        value = float(rpm)
        if not math.isfinite(value) or value <= 0:
            raise DatasetError(f"实测转速必须是大于 0 的数值，收到：{rpm!r}")
        model.measured_rpm = value
    session.commit()


def delete_dataset(session: Session, dataset_id: int, operator: str = "mark") -> dict[str, int]:
    """软删除数据集：同一事务内标记数据集 / 通道 / 指标值，并写一条 deletion_log。

    返回级联条数（含自己）：``{"dataset": 1, "channel": n, "metric_value": m}``，
    回收站按这条留痕恢复时一并复原。
    """
    begin_immediate(session)  # 先读后写：先取写锁，免得并发时事务升级失败
    model = session.get(Dataset, dataset_id)
    if not _alive(model):
        raise DatasetError(f"数据集不存在：{dataset_id}")
    now = datetime.now()
    channels = list(
        session.execute(
            select(Channel).where(Channel.dataset_id == dataset_id, Channel.deleted_at.is_(None))
        ).scalars()
    )
    values = 0
    for child in channels:
        points = list(
            session.execute(
                select(MetricValue).where(
                    MetricValue.channel_id == child.id, MetricValue.deleted_at.is_(None)
                )
            ).scalars()
        )
        for point in points:
            point.deleted_at = now
        values += len(points)
        child.deleted_at = now
    model.deleted_at = now

    counts = {"dataset": 1, "channel": len(channels), "metric_value": values}
    session.add(
        DeletionLog(
            entity_type="dataset",
            entity_id=model.id,
            entity_name=f"{model.code} {model.name}",
            mode="soft",
            cascade_counts=json.dumps(counts, ensure_ascii=False),
            operator=operator,
        )
    )
    session.flush()
    return counts


@dataclass(slots=True)
class BadSourceFile:
    """导入目录里读不了的文件（头部解析失败 / 采样率或 scale 非法）：文件名 + 原因。"""

    filename: str
    reason: str


def _check_channel_duplicates(metas: list) -> None:
    """同批重复通道号预检：一个批次里同一通道号只能有一个文件（复查 B3）。

    ``channel`` 表对 ``(dataset_id, code)`` 有唯一约束，重名文件写库时才会炸
    ``IntegrityError``；这里在**写库前**就拦住，给出能操作的中文说明。
    """
    by_channel: dict[str, list[str]] = {}
    for meta in metas:
        by_channel.setdefault(meta.channel, []).append(meta.path.name)
    for code, names in by_channel.items():
        if len(names) > 1:
            raise DatasetError(
                f"同一批次里通道号重复：{code} 出现在 {'、'.join(sorted(names))}；"
                "请把重复通道的文件分到不同目录（或改名）后再导入"
            )


def import_from_dir(
    session: Session, task_id: int, directory: str | Path, name: str | None = None
) -> DatasetRow:
    """导入一个采集目录：同任务 + 同目录 + 同起始时间视为同一数据集（幂等）。

    这是一个「事务拥有者」入口：建数据集 + 建通道文件在本函数末尾**一次提交**
    （store 层各写函数只 flush，见 P1 / B2）。批次指标值不在这里算，走
    :func:`lion_monitor.features.pipeline.import_batch` 的批次事务。
    """
    row, problems = import_from_dir_detail(session, task_id, directory, name)
    if row is None:
        # 简单入口保持原口径：整个目录读不了一个字中文错误（导入链路走 detail 版降级）
        detail = "；".join(f"{item.filename}（{item.reason}）" for item in problems[:3])
        raise DatasetError(f"目录里的文件都读不了：{detail}")
    return row


def import_from_dir_detail(
    session: Session,
    task_id: int,
    directory: str | Path,
    name: str | None = None,
    commit: bool = True,
) -> tuple[DatasetRow | None, list[BadSourceFile]]:
    """同上，另外返回「目录里读不了的文件」（导入链路要把它记进 files_bad）。

    坏文件（头部解析失败 / 采样率或 scale 非法 / 通道号为空）只降级该文件，
    不打断整批；目录里一个可用文件都没有时返回 ``(None, problems)``，交给导入链路
    按「整批不建数据集」处理（复查 G8：不抛异常，页面只给一条中文提示）。
    目录里根本没有 .dat 文件时仍抛 :class:`DatasetError`。

    ``commit=False`` 交给批次事务统一提交（``features.pipeline.import_batch`` 用），
    此时失败整体回滚、库内 0 行新增。
    """
    folder = Path(directory)
    if not folder.is_dir():
        raise DatasetError(f"目录不存在：{folder}")

    metas, bad_headers = scan_vibration_dir_checked(folder)
    problems = [BadSourceFile(filename=item.filename, reason=item.reason) for item in bad_headers]
    if not metas:
        if problems:
            return None, problems
        raise DatasetError(f"目录里没有 .dat 文件：{folder}")
    _check_channel_duplicates(metas)

    started = max(meta.started_at for meta in metas)
    # 路径口径统一到 norm_dir（复查 G1）：页面「已处理」索引也用 norm_dir，
    # 相对路径 / 绝对路径 / 结尾斜杠的同一目录只能对应一个数据集。
    normalized = norm_dir(folder)
    candidates = session.execute(
        select(Dataset).where(
            Dataset.task_id == task_id,
            Dataset.started_at == started,
            Dataset.deleted_at.is_(None),
        )
    ).scalars()
    existing = next(
        (
            model
            for model in candidates
            if model.source_dir and norm_dir(model.source_dir) == normalized
        ),
        None,
    )
    if existing is not None:
        return get_dataset(session, existing.id), problems

    row = create_dataset(
        session,
        task_id,
        name or Path(normalized).name or normalized,
        normalized,
        started.strftime(_TIME_FMT),
    )
    for meta in metas:
        add_file(
            session,
            row.id,
            meta.path,
            meta.channel,
            meta.sample_rate_hz,
            meta.sample_count,
        )
    if commit:
        session.commit()
    return get_dataset(session, row.id), problems
