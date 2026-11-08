"""原始波形上屏：区间读取 + 最小/最大包络抽样 + CSV 导出（方案 9.12）。

口径（现场定，方案 9.12「波形抽稀规则」）：

- 区间点数 ≤ 显示上限 → 原样逐点画，不抽稀；
- 超过上限 → **最小/最大包络抽样**：序列按等长分箱，每箱取最小值与最大值两点，
  保证冲击/尖峰不会被跳过（等间隔抽点会漏峰，不采用）；
- CSV 导出始终是原始逐点值（时间, 值），不受抽样影响；
- 纵轴单位两选一：加速度 ``a (g)`` / 速度 ``v (mm/s)``（速度口径复用
  ``features.metrics.velocity_series``：10–1000 Hz 带通 + 频域积分）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..ingest.vibration import VibrationSlice, read_vibration_range
from .metrics import velocity_series

__all__ = [
    "DEFAULT_MAX_POINTS",
    "UNIT_ACCEL",
    "UNIT_VELOCITY",
    "WAVE_UNITS",
    "WaveformSeries",
    "decimate_indices",
    "load_waveform",
    "sample_points",
]

#: 纵轴单位选项（页面下拉直接用这两个字符串）
UNIT_ACCEL = "加速度 a (g)"
UNIT_VELOCITY = "速度 v (mm/s)"
WAVE_UNITS = (UNIT_ACCEL, UNIT_VELOCITY)

#: 显示上限点数默认值：32 kHz / 20 s 的 640,000 点等于不抽稀
DEFAULT_MAX_POINTS = 1_000_000

#: 积分成速度前左右各多读的边距（秒）：抵消 10 Hz 低端振铃，与 metrics 的边缘裁剪同量级
VELOCITY_EDGE_PAD_S = 0.5


@dataclass(frozen=True, slots=True)
class WaveformSeries:
    """一段区间内的原始逐点波形（未抽稀）。"""

    label: str
    unit: str
    sample_rate: float
    start_s: float
    times: np.ndarray
    values: np.ndarray

    @property
    def point_count(self) -> int:
        """区间原始点数。"""
        return int(self.values.size)

    def csv_text(self) -> str:
        """原始逐点 CSV（时间 s, 值），永远不抽稀。"""
        head = f"时间 (s),{self.unit}"
        rows = [
            f"{t:.6f},{v:.9g}"
            for t, v in zip(self.times.tolist(), self.values.tolist(), strict=True)
        ]
        return "\n".join([head, *rows]) + "\n"


def load_waveform(
    path: str | Path,
    start_s: float,
    end_s: float | None,
    *,
    unit: str = UNIT_ACCEL,
    label: str = "",
) -> WaveformSeries:
    """读 ``path`` 的 ``start_s``~``end_s`` 区间，按 ``unit`` 给出原始逐点波形。"""
    if unit not in WAVE_UNITS:
        raise ValueError(f"未知纵轴单位：{unit}（可选 {' / '.join(WAVE_UNITS)}）")
    chunk = read_vibration_range(path, start_s, end_s)
    values = np.asarray(chunk.samples, dtype="float64")
    if unit == UNIT_VELOCITY:
        values = _velocity_of_range(path, chunk)
    return WaveformSeries(
        label=label or chunk.meta.channel,
        unit=unit,
        sample_rate=chunk.meta.sample_rate_hz,
        start_s=chunk.start_s,
        times=chunk.time_axis(),
        values=values,
    )


def _velocity_of_range(path: str | Path, chunk: VibrationSlice) -> np.ndarray:
    """区间速度序列（mm/s）：左右各多读 ``VELOCITY_EDGE_PAD_S`` 秒再积分，裁回区间。

    10 Hz 低端振铃经 1/ω 放大成首尾巨大漂移（现场实测 ±50 mm/s，真实包络只有 ±12），
    多读边距后积分结果与「整段一次算」一致，图上看不到假尖峰；时间轴仍是请求区间。
    """
    fs = chunk.meta.sample_rate_hz
    lo = max(0.0, chunk.start_s - VELOCITY_EDGE_PAD_S)
    hi = min(chunk.meta.duration_s, chunk.start_s + chunk.duration_s + VELOCITY_EDGE_PAD_S)
    padded = read_vibration_range(path, lo, hi)
    series = velocity_series(np.asarray(padded.samples, dtype="float64"), fs)
    offset = int(round((chunk.start_s - padded.start_s) * fs))
    cropped = series[offset : offset + chunk.samples.size]
    if cropped.size != chunk.samples.size:  # 兜底：边距被文件时长截断时的极端情形
        return velocity_series(np.asarray(chunk.samples, dtype="float64"), fs)
    return cropped


def decimate_indices(values: np.ndarray, cap: int) -> np.ndarray:
    """最小/最大包络抽样：等长分箱，返回每箱最小值与最大值的索引。

    ``len(values) <= cap`` 时原样返回全部索引；超上限时输出约 ``cap`` 个点
    （分 ``cap // 2`` 个箱，每箱两点），冲击尖峰不会被跳过。
    """
    x = np.asarray(values)
    n = int(x.size)
    if cap < 2:
        raise ValueError(f"显示上限点数至少 2，当前 {cap}")
    if n <= cap:
        return np.arange(n, dtype=np.int64)

    edges = np.unique(np.linspace(0, n, max(2, cap // 2) + 1).astype(np.int64))
    picked: list[int] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        segment = x[lo:hi]
        imin = int(np.argmin(segment))
        imax = int(np.argmax(segment))
        picked.append(lo + imin)
        if imax != imin:
            picked.append(lo + imax)
    return np.asarray(picked, dtype=np.int64)


def sample_points(
    series: WaveformSeries, cap: int = DEFAULT_MAX_POINTS
) -> tuple[np.ndarray, np.ndarray, bool]:
    """按显示上限决定绘图点：返回 (时间, 值, 是否抽稀)。"""
    idx = decimate_indices(series.values, cap)
    decimated = idx.size < series.values.size
    return series.times[idx], series.values[idx], decimated
