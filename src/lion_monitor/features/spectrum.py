"""FFT 幅值谱（方案 9.12）：窗函数 + 去均值 + Peak/RMS + 主峰 + 阶次换算 + CSV。

口径（马哥定，方案 9.12）：

- 对选中区间**整段一次 FFT**（不叠段、不设重叠率），出单边幅值谱 ``0 ~ fs/2``；
- 窗函数六选一，映射到 ``scipy.signal.get_window``（凯泽窗固定 β=8.6）；
- 幅值按**窗函数相干增益**归一：``|X| * 2 / (N * cg)``，``cg = sum(w)/N``，
  直流分箱不翻倍；因此加窗后峰值仍是该频率正弦分量的幅值（矩形窗与汉宁窗差异 < 2%）；
- 幅值类型 ``峰值 Peak`` / ``有效值 RMS = Peak/√2``；
- 去均值默认勾选：先减掉样本均值，0 Hz 处不再出现直流大尖峰；
- 峰值只列相对主峰 ≥ 1%（-40 dB）的显著峰，按幅值降序，峰间距至少 5 个分箱
  （避免把同一主瓣采成多个峰）；
- 阶次换算基准 ``f0`` = 「目标频率」第一个值，第 n 阶画在 ``n × f0``；
- CSV 导出固定两列：``频率 (Hz), 幅值 (<单位>)``。

选型：窗函数与峰值检测复用成熟库（``scipy.signal.get_window`` / ``find_peaks``），
自己只写相干增益归一这一薄层——``scipy.signal.periodogram(scaling="spectrum")``
给的是 RMS² 口径，与本页默认的峰值口径不符，直接套会差 √2。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import find_peaks, get_window

from ..ingest.vibration import read_vibration_range

__all__ = [
    "AMPLITUDE_TYPES",
    "DEFAULT_PEAKS",
    "DEFAULT_WINDOW",
    "PEAK_FLOOR_REL",
    "SPECTRUM_PEAK",
    "SPECTRUM_RMS",
    "WINDOW_LABELS",
    "WINDOWS",
    "OrderMark",
    "Peak",
    "Spectrum",
    "fft_spectrum",
    "load_spectrum",
    "order_mark_lines",
    "spectrum_from_samples",
    "top_peaks",
    "window_values",
]

#: 幅值类型（页面下拉直接用这两个字符串）
SPECTRUM_PEAK = "峰值 Peak"
SPECTRUM_RMS = "有效值 RMS"
AMPLITUDE_TYPES = (SPECTRUM_PEAK, SPECTRUM_RMS)

#: 窗函数：界面名 → ``scipy.signal.get_window`` 的参数
WINDOWS: dict[str, Any] = {
    "汉宁窗 Hann": "hann",
    "平顶窗 Flattop": "flattop",
    "海明窗 Hamming": "hamming",
    "布莱克曼窗 Blackman": "blackman",
    "凯泽窗 Kaiser（β=8.6）": ("kaiser", 8.6),
    "矩形窗 Rect": "boxcar",
}
WINDOW_LABELS = tuple(WINDOWS)
DEFAULT_WINDOW = "汉宁窗 Hann"

#: 峰值列表条数（主峰 + 次峰，图上 ▲ 与列表都用它）
DEFAULT_PEAKS = 5

#: 峰幅值下限（相对主峰）：低于 -40 dB 的算底噪，不列峰
PEAK_FLOOR_REL = 1e-2

#: 相邻峰最小间隔（分箱数）：汉宁主瓣宽约 4 bin，取 5 避免同一主瓣采成多峰
MIN_PEAK_GAP_BINS = 5


def window_values(name: str, size: int) -> np.ndarray:
    """按界面名取窗函数序列（长度 ``size``，周期性 FFT 窗）。"""
    if name not in WINDOWS:
        raise ValueError(f"未知窗函数：{name}（可选 {' / '.join(WINDOW_LABELS)}）")
    if size < 2:
        raise ValueError(f"样本数至少 2，当前 {size}")
    return np.asarray(get_window(WINDOWS[name], int(size), fftbins=True), dtype=np.float64)


def fft_spectrum(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    window: str = DEFAULT_WINDOW,
    demean: bool = True,
    amplitude_type: str = SPECTRUM_PEAK,
) -> tuple[np.ndarray, np.ndarray]:
    """单边幅值谱：返回 (频率 Hz, 幅值)，幅值已按窗函数相干增益归一。"""
    if amplitude_type not in AMPLITUDE_TYPES:
        raise ValueError(f"未知幅值类型：{amplitude_type}（可选 {' / '.join(AMPLITUDE_TYPES)}）")
    if sample_rate_hz <= 0:
        raise ValueError(f"采样率必须为正，当前 {sample_rate_hz}")
    x = np.asarray(samples, dtype=np.float64)
    n = int(x.size)
    if n < 2:
        raise ValueError(f"样本数至少 2，当前 {n}")

    if demean:
        x = x - x.mean()
    win = window_values(window, n)
    coherent_gain = float(win.sum()) / n
    if coherent_gain <= 0:
        raise ValueError(f"窗函数相干增益非正：{coherent_gain}")

    amplitude = np.abs(np.fft.rfft(x * win)) * 2.0 / (n * coherent_gain)
    amplitude[0] /= 2.0
    if amplitude_type == SPECTRUM_RMS:
        amplitude = amplitude / np.sqrt(2.0)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate_hz)
    return freqs, amplitude


@dataclass(frozen=True, slots=True)
class Peak:
    """频谱里的一个峰。"""

    freq_hz: float
    amplitude: float

    def text(self, unit: str = "g", *, freq_decimals: int = 2) -> str:
        """「145.00 Hz / 0.5 g」形式的显示文本。"""
        return f"{self.freq_hz:.{freq_decimals}f} Hz / {self.amplitude:.4g} {unit}"


def top_peaks(
    freqs: np.ndarray,
    amplitudes: np.ndarray,
    *,
    count: int = DEFAULT_PEAKS,
    floor_rel: float = PEAK_FLOOR_REL,
) -> tuple[Peak, ...]:
    """按幅值降序挑显著峰，返回 ``Peak`` 元组（首项即主峰）。"""
    f = np.asarray(freqs, dtype=np.float64)
    a = np.asarray(amplitudes, dtype=np.float64)
    if f.size != a.size:
        raise ValueError(f"频率与幅值长度不一致: {f.size} vs {a.size}")
    if f.size < 3 or count <= 0:
        return ()

    distance = max(1, int(round(MIN_PEAK_GAP_BINS)))
    candidates, _ = find_peaks(a, distance=distance)
    candidates = candidates[f[candidates] > 0]  # 0 Hz 直流不算峰
    if candidates.size == 0:
        return ()

    ceiling = float(a[candidates].max())
    candidates = candidates[a[candidates] >= ceiling * floor_rel]
    winner = candidates[np.argsort(a[candidates])[::-1][:count]]
    return tuple(
        Peak(freq_hz=float(f[i]), amplitude=float(a[i]))
        for i in sorted(winner, key=lambda i: -a[i])
    )


@dataclass(frozen=True, slots=True)
class OrderMark:
    """一条阶次竖线：第 ``group`` 个目标频率（基准）的第 ``order`` 阶。

    ``group`` 用来给「同一目标频率的多个阶次」上同一种颜色（马哥 2026-11-03）。
    """

    order: float
    freq_hz: float
    group: int


def order_mark_lines(base_freqs: Sequence[float], orders: Sequence[float]) -> list[OrderMark]:
    """每个目标频率都按同一组阶次画线：n 阶落在 ``n × f0``。

    多个目标频率时逐组展开（马哥 2026-11-03：每个目标频率都要标出对应的阶数），
    ``group`` 记录它属于哪个基准频率，绘图按组上色。
    没有目标频率（也就没有基准频率）时返回空列表——界面据此提示先填基准频率。
    """
    marks: list[OrderMark] = []
    for group, base in enumerate(float(value) for value in base_freqs):
        if base <= 0:
            continue
        for order in orders:
            marks.append(OrderMark(float(order), float(order) * base, group))
    return sorted(marks, key=lambda mark: mark.freq_hz)


@dataclass(frozen=True, slots=True)
class Spectrum:
    """一段区间的一次 FFT 结果（单边幅值谱 + 显著峰）。"""

    label: str
    unit: str
    sample_rate: float
    start_s: float
    sample_count: int
    window: str
    amplitude_type: str
    freqs: np.ndarray
    amplitudes: np.ndarray
    peaks: tuple[Peak, ...]

    @property
    def resolution_hz(self) -> float:
        """频率分辨率 Δf = fs / N。"""
        return self.sample_rate / self.sample_count if self.sample_count else 0.0

    def main_peak(self) -> Peak | None:
        """主峰（幅值最大的显著峰）。"""
        return self.peaks[0] if self.peaks else None

    def csv_text(self) -> str:
        """逐频点 CSV（频率 Hz, 幅值），不受峰值筛选影响。"""
        head = f"频率 (Hz),幅值 ({self.unit})"
        rows = [
            f"{freq:.6f},{amp:.9g}"
            for freq, amp in zip(self.freqs.tolist(), self.amplitudes.tolist(), strict=True)
        ]
        return "\n".join([head, *rows]) + "\n"


def spectrum_from_samples(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    label: str = "",
    unit: str = "g",
    start_s: float = 0.0,
    window: str = DEFAULT_WINDOW,
    demean: bool = True,
    amplitude_type: str = SPECTRUM_PEAK,
    peaks: int = DEFAULT_PEAKS,
) -> Spectrum:
    """对一段样本做一次 FFT，返回 ``Spectrum``（含显著峰列表）。"""
    freqs, amplitudes = fft_spectrum(
        samples,
        sample_rate_hz,
        window=window,
        demean=demean,
        amplitude_type=amplitude_type,
    )
    return Spectrum(
        label=label,
        unit=unit,
        sample_rate=float(sample_rate_hz),
        start_s=float(start_s),
        sample_count=int(np.asarray(samples).size),
        window=window,
        amplitude_type=amplitude_type,
        freqs=freqs,
        amplitudes=amplitudes,
        peaks=top_peaks(freqs, amplitudes, count=peaks),
    )


def load_spectrum(
    path: str | Path,
    start_s: float,
    end_s: float | None,
    *,
    label: str = "",
    unit: str = "g",
    window: str = DEFAULT_WINDOW,
    demean: bool = True,
    amplitude_type: str = SPECTRUM_PEAK,
    peaks: int = DEFAULT_PEAKS,
) -> Spectrum:
    """读 ``path`` 的 ``start_s``~``end_s`` 区间做一次 FFT（整段一次，不分段）。"""
    chunk = read_vibration_range(path, start_s, end_s)
    return spectrum_from_samples(
        np.asarray(chunk.samples, dtype=np.float64),
        chunk.meta.sample_rate_hz,
        label=label or chunk.meta.channel,
        unit=unit,
        start_s=chunk.start_s,
        window=window,
        demean=demean,
        amplitude_type=amplitude_type,
        peaks=peaks,
    )
