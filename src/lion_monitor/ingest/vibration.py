"""振动 .dat 文件读取接口。

文件格式（实测于 2026-10-28，现场样本 CH3_Task1.dat）：

- 头部 512 字节，字段布局见 ``_HEADER_LAYOUT``
- 数据体：float32 little-endian，紧接头部
- 采样点数在头部偏移 0x0F0，采样率在 0x110

设计原则：头部不合法或长度不一致时直接报错，绝不静默截断数据。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

__all__ = [
    "HEADER_SIZE",
    "SUPPORTED_FORMAT_VERSIONS",
    "BadHeaderFile",
    "VibrationFormatError",
    "VibrationMeta",
    "VibrationRecord",
    "read_vibration",
    "read_vibration_header",
    "scan_vibration_dir",
    "scan_vibration_dir_checked",
]

#: 头部固定长度（字节）
HEADER_SIZE = 512

#: 支持的头版本号（头部偏移 0x00 的 uint16）
SUPPORTED_FORMAT_VERSIONS = frozenset({20})

#: 头部字段偏移：名称 -> (偏移, struct 格式)
_HEADER_LAYOUT: dict[str, tuple[int, str]] = {
    "format_version": (0x00, "<H"),
    "scale": (0x08, "<f"),
    "sample_count": (0x0F0, "<I"),
    "sample_rate_hz": (0x110, "<f"),
}

#: 字符串槽位：名称 -> 槽位序号（每槽 16 字节，从 0x50 起）
_STRING_SLOTS: dict[str, int] = {
    "rule": 0,
    "site": 1,
    "device_ip": 2,
    "channel": 3,
    "started_at": 8,
}
_STRING_BASE = 0x50
_STRING_WIDTH = 16
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


class VibrationFormatError(ValueError):
    """振动文件格式不合法（头部损坏、版本不支持、长度不一致等）。"""


@dataclass(frozen=True, slots=True)
class VibrationMeta:
    """振动采集文件的元数据（不含样本数据）。"""

    path: Path
    channel: str
    sample_rate_hz: float
    sample_count: int
    started_at: datetime
    device_ip: str
    site: str
    rule: str
    format_version: int
    scale: float

    @property
    def duration_s(self) -> float:
        """记录时长（秒）；采样率非法/非有限时返回 0（扫描阶段不许因坏头除零崩溃）。"""
        fs = self.sample_rate_hz
        if not math.isfinite(fs) or fs <= 0:
            return 0.0
        return self.sample_count / fs


def _check_header_values(meta: VibrationMeta) -> None:
    """头部数值校验：坏头不许建 channel 行（复查 B3）。

    ``sample_rate_hz`` / ``metric_value.value`` 都是非空列，NaN 会以 NULL 落库 →
    ``IntegrityError`` 打在页面上；所以在入库之前就把这类文件判成坏文件。
    """
    name = meta.path.name
    rate = meta.sample_rate_hz
    if not math.isfinite(rate) or rate <= 0:
        raise VibrationFormatError(f"{name}: 采样率不合法（{rate}），应为正数")
    if not math.isfinite(meta.scale):
        raise VibrationFormatError(f"{name}: 头部 scale 非法（{meta.scale}），无法换算出工程值")
    if not meta.channel.strip():
        raise VibrationFormatError(f"{name}: 头部通道号为空")


def _check_samples(samples: np.ndarray, name: str) -> None:
    """样本必须全部有限：NaN/Inf 会让指标值和判级一起坏掉（复查 B3 / G10）。"""
    if samples.size and not bool(np.isfinite(samples).all()):
        raise VibrationFormatError(f"{name}: 样本含非有限值（NaN/Inf），已跳过该文件")


def _decode_string(header: bytes, slot: int) -> str:
    start = _STRING_BASE + slot * _STRING_WIDTH
    raw = header[start : start + _STRING_WIDTH]
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="strict").strip()


def read_vibration_header(path: str | Path) -> VibrationMeta:
    """只读取 512 字节头部，返回元数据（不加载数据体）。"""
    path = Path(path)
    with path.open("rb") as fh:
        header = fh.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE:
        raise VibrationFormatError(
            f"{path.name}: 文件长度 {len(header)} B 不足头部 {HEADER_SIZE} B"
        )

    fields = {
        name: struct.unpack_from(fmt, header, off)[0] for name, (off, fmt) in _HEADER_LAYOUT.items()
    }
    if fields["format_version"] not in SUPPORTED_FORMAT_VERSIONS:
        raise VibrationFormatError(
            f"{path.name}: 不支持的头版本 {fields['format_version']}，"
            f"支持 {sorted(SUPPORTED_FORMAT_VERSIONS)}"
        )

    strings = {name: _decode_string(header, slot) for name, slot in _STRING_SLOTS.items()}
    try:
        started_at = datetime.strptime(strings["started_at"], _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise VibrationFormatError(
            f"{path.name}: 文件头时间不可解析: {strings['started_at']!r}"
        ) from exc

    return VibrationMeta(
        path=path,
        channel=strings["channel"],
        sample_rate_hz=float(fields["sample_rate_hz"]),
        sample_count=int(fields["sample_count"]),
        started_at=started_at,
        device_ip=strings["device_ip"],
        site=strings["site"],
        rule=strings["rule"],
        format_version=int(fields["format_version"]),
        scale=float(fields["scale"]),
    )


@dataclass(frozen=True, slots=True)
class VibrationRecord:
    """一次振动采集的完整记录：元数据 + 样本序列。"""

    meta: VibrationMeta
    samples: np.ndarray

    @property
    def duration_s(self) -> float:
        """记录时长（秒）。"""
        return self.meta.duration_s

    def time_axis(self) -> np.ndarray:
        """样本对应的相对时间轴（秒），从 0 开始。"""
        n = self.meta.sample_count
        return np.arange(n, dtype=np.float64) / self.meta.sample_rate_hz


def _check_body_size(meta: VibrationMeta) -> None:
    """数据体长度必须与头部声明的样本数一致，否则报错（绝不静默截断）。"""
    expected = HEADER_SIZE + 4 * meta.sample_count
    actual = meta.path.stat().st_size
    if actual != expected:
        raise VibrationFormatError(
            f"{meta.path.name}: 文件长度不一致，头部声明 {meta.sample_count} 个样本"
            f"应为 {expected} B（{HEADER_SIZE} B 头 + {meta.sample_count}×4 B），实际 {actual} B"
        )


@dataclass(frozen=True, slots=True)
class VibrationSlice:
    """文件中的一段区间：元数据 + 该区间的工程值样本（不含区间外数据）。"""

    meta: VibrationMeta
    start_s: float
    samples: np.ndarray

    @property
    def duration_s(self) -> float:
        """区间时长（秒）。"""
        return self.samples.size / self.meta.sample_rate_hz

    def time_axis(self) -> np.ndarray:
        """区间样本的时间轴（秒，从文件起点开始计时）。"""
        offset = np.arange(self.samples.size, dtype=np.float64) / self.meta.sample_rate_hz
        return self.start_s + offset


def read_vibration_range(
    path: str | Path, start_s: float = 0.0, end_s: float | None = None
) -> VibrationSlice:
    """只读取 ``start_s`` ~ ``end_s`` 这段样本。

    按字节偏移定位，不加载整个文件；``end_s=None`` 读到文件末尾，超界按文件时长截断。
    样本单位口径与 :func:`read_vibration` 一致（文件头 ``scale``）。区间为空或起点为负直接报错。
    """
    meta = read_vibration_header(path)
    _check_header_values(meta)
    if start_s < 0:
        raise ValueError(f"开始时间不能为负：{start_s} s")
    fs = meta.sample_rate_hz
    total = meta.sample_count
    i0 = min(max(int(round(start_s * fs)), 0), total)
    i1 = total if end_s is None else min(max(int(round(end_s * fs)), 0), total)
    if i1 <= i0:
        raise ValueError(
            f"区间为空：{start_s:g}~{end_s if end_s is not None else meta.duration_s:g} s"
            f"（文件时长 {meta.duration_s:g} s）"
        )
    _check_body_size(meta)
    samples = np.fromfile(meta.path, dtype="<f4", offset=HEADER_SIZE + 4 * i0, count=i1 - i0)
    if meta.scale != 1.0:
        samples = (samples.astype(np.float64) * meta.scale).astype(np.float32)
    samples = samples.astype(np.float32, copy=False)
    _check_samples(samples, meta.path.name)
    samples.flags.writeable = False
    return VibrationSlice(meta=meta, start_s=i0 / fs, samples=samples)


def read_vibration(path: str | Path) -> VibrationRecord:
    """读取完整振动记录（元数据 + float32 样本）。

    样本单位以文件头 ``scale`` 为准：返回的 ``samples`` 已乘过 ``scale``，
    即工程值 = 文件里的原始 float32 值 × ``scale``（当前现场的 .dat 为 1.0，
    值本身已是加速度 g）。传感器灵敏度（如 500 mV/g）只作台账记录，
    不参与此处换算。
    """
    meta = read_vibration_header(path)
    _check_header_values(meta)
    _check_body_size(meta)
    samples = np.fromfile(meta.path, dtype="<f4", offset=HEADER_SIZE, count=meta.sample_count)
    if meta.scale != 1.0:
        samples = (samples.astype(np.float64) * meta.scale).astype(np.float32)
    samples = samples.astype(np.float32, copy=False)
    _check_samples(samples, meta.path.name)
    samples.flags.writeable = False
    return VibrationRecord(meta=meta, samples=samples)


@dataclass(frozen=True, slots=True)
class BadHeaderFile:
    """头部读不了的文件：路径 + 原因（导入时降级成 files_bad，不打断整批）。"""

    path: Path
    reason: str

    @property
    def filename(self) -> str:
        """文件名（页面点名用）。"""
        return self.path.name


def scan_vibration_dir_checked(
    directory: str | Path, pattern: str = "*.dat"
) -> tuple[list[VibrationMeta], list[BadHeaderFile]]:
    """扫描目录：单个文件头部不合法只记进坏文件列表，不打断整批（复查 B3）。

    返回 ``(可用文件元数据, 坏头文件)``；元数据按采集起始时间升序。
    头部数值（采样率 / scale / 通道号）在这里就校验，坏文件不会进入后续入库流程。
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"目录不存在: {directory}")
    metas: list[VibrationMeta] = []
    bad: list[BadHeaderFile] = []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file():
            continue
        try:
            meta = read_vibration_header(path)
            _check_header_values(meta)
        except (VibrationFormatError, OSError, ValueError) as exc:
            bad.append(BadHeaderFile(path=path, reason=str(exc).strip() or type(exc).__name__))
            continue
        metas.append(meta)
    metas.sort(key=lambda m: (m.started_at, m.path.name))
    return metas, bad


def scan_vibration_dir(directory: str | Path, pattern: str = "*.dat") -> list[VibrationMeta]:
    """扫描目录下所有振动文件，只读头部，按采集起始时间升序返回。

    严格口径（保持原行为）：有任何一个文件头部不合法就抛 :class:`VibrationFormatError`；
    导入链路要「单文件降级」请用 :func:`scan_vibration_dir_checked`。
    """
    metas, bad = scan_vibration_dir_checked(directory, pattern)
    if bad:
        raise VibrationFormatError(bad[0].reason)
    return metas
