"""包络谱（方案 9.12）：频域带通 → 希尔伯特取模得包络 → 去均值 → FFT → 包络谱。

口径（现场定，方案 9.12）：

- 包络方法**固定希尔伯特**（解析信号模 ``|hilbert(x)|``），界面不给下拉、不显示方法文字；
- 带通为**频域滤波**（``rfft`` → 带外置零 → ``irfft``，见 ``features.filters``），
  与指标层 ``v_RMS``/``a_RMS`` 同一实现：零相位、无 IIR 暂态、**没有阶数设置项**
  （2026-11-03：包络谱滤波采用频域滤波，复用已有算法，删除滤波器阶数选项）；
- 带通边界语义：下限 0 且上限 fs/2 → **全频带，等效不做带通**（默认参数）；
  只给下限（上限 fs/2）→ 高通；只给上限（下限 0）→ 低通；
- 上限 ≤ 下限、边界为负、上限超过 fs/2 → 直接抛 ``ValueError``，
  页面据此告警且不计算（不产生 NaN 图）；
- 去均值默认勾选，作用对象是**包络信号**：先减掉包络均值，0 Hz 直流峰消失，
  而 145/290 Hz 这类特征峰幅值不变；
- 包络谱的幅值口径与 FFT 幅值谱一致：汉宁窗 + 窗函数相干增益归一 ``|X|·2/(N·cg)``，
  幅值恒为峰值 Peak（包络谱不提供有效值开关）；横轴 ``0 ~ fs/2``，分辨率 ``Δf = fs/N``；
- CSV 导出固定两列：``频率 (Hz), 幅值 (<单位>)``。

选型：包络本身复用成熟库 ``scipy.signal.hilbert``（振动包络分析的事实标准），
带通复用项目内已有的频域带通 ``features.filters.bandpass_freq``（指标层同一份），
自研只做业务封装（参数校验 + 口径统一 + 接线），不手写滤波器。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.signal import hilbert

from ..ingest.vibration import read_vibration_range
from .filters import bandpass_freq
from .spectrum import (
    DEFAULT_PEAKS,
    DEFAULT_WINDOW,
    SPECTRUM_PEAK,
    Spectrum,
    fft_spectrum,
    top_peaks,
)

__all__ = [
    "bandpass_filter",
    "envelope_spectrum_from_samples",
    "hilbert_envelope",
    "load_envelope_spectrum",
]


def bandpass_filter(
    samples: np.ndarray,
    sample_rate_hz: float,
    low_hz: float,
    high_hz: float,
) -> np.ndarray:
    """频域带通：返回 ``low_hz ~ high_hz`` 的滤波结果（float64）。

    实现即 ``features.filters.bandpass_freq``（指标层 v_RMS/a_RMS 用的同一份）：
    零相位、无 IIR 暂态、没有阶数。
    ``low_hz = 0`` 且 ``high_hz = fs/2`` 时不做滤波（全频带，原样返回）。
    参数越界一律抛 ``ValueError``，绝不返回 NaN。
    """
    if sample_rate_hz <= 0:
        raise ValueError(f"采样率必须为正，当前 {sample_rate_hz}")
    if low_hz < 0:
        raise ValueError(f"带通下限不能为负，当前 {low_hz:g} Hz")
    nyquist = float(sample_rate_hz) / 2.0
    if high_hz > nyquist:
        raise ValueError(f"带通上限不能超过 fs/2 = {nyquist:g} Hz，当前 {high_hz:g} Hz")
    if high_hz <= low_hz:
        raise ValueError(f"带通上限必须大于下限，当前 {low_hz:g} ~ {high_hz:g} Hz")

    return bandpass_freq(np.asarray(samples, dtype=np.float64), sample_rate_hz, low_hz, high_hz)


def hilbert_envelope(samples: np.ndarray) -> np.ndarray:
    """希尔伯特包络：解析信号的模 ``|x + j·H(x)|``（非负、无相位偏移）。"""
    x = np.asarray(samples, dtype=np.float64)
    if x.size < 2:
        raise ValueError(f"样本数至少 2，当前 {x.size}")
    return np.abs(hilbert(x))


def envelope_spectrum_from_samples(
    samples: np.ndarray,
    sample_rate_hz: float,
    *,
    label: str = "",
    unit: str = "g",
    start_s: float = 0.0,
    band_low_hz: float = 0.0,
    band_high_hz: float | None = None,
    demean: bool = True,
    peaks: int = DEFAULT_PEAKS,
    window: str = DEFAULT_WINDOW,
) -> Spectrum:
    """一段样本的包络谱：带通 → 希尔伯特包络 → （去均值）→ FFT。

    ``band_high_hz=None`` 取 ``fs/2``（默认全频带）。返回的 ``Spectrum`` 是**包络信号**的
    幅值谱（幅值口径恒为峰值 Peak），带通参数由调用方（页面）自己记着显示。
    """
    nyquist = float(sample_rate_hz) / 2.0
    high_hz = nyquist if band_high_hz is None else float(band_high_hz)
    filtered = bandpass_filter(samples, sample_rate_hz, float(band_low_hz), high_hz)
    envelope = hilbert_envelope(filtered)
    freqs, amplitudes = fft_spectrum(
        envelope,
        sample_rate_hz,
        window=window,
        demean=demean,
        amplitude_type=SPECTRUM_PEAK,
    )
    return Spectrum(
        label=label,
        unit=unit,
        sample_rate=float(sample_rate_hz),
        start_s=float(start_s),
        sample_count=int(envelope.size),
        window=window,
        amplitude_type=SPECTRUM_PEAK,
        freqs=freqs,
        amplitudes=amplitudes,
        peaks=top_peaks(freqs, amplitudes, count=peaks),
    )


def load_envelope_spectrum(
    path: str | Path,
    start_s: float,
    end_s: float | None,
    *,
    label: str = "",
    unit: str = "g",
    band_low_hz: float = 0.0,
    band_high_hz: float | None = None,
    demean: bool = True,
    peaks: int = DEFAULT_PEAKS,
    window: str = DEFAULT_WINDOW,
) -> Spectrum:
    """读 ``path`` 的 ``start_s``~``end_s`` 区间做包络谱（整段一次，不分段）。"""
    chunk = read_vibration_range(path, start_s, end_s)
    return envelope_spectrum_from_samples(
        np.asarray(chunk.samples, dtype=np.float64),
        chunk.meta.sample_rate_hz,
        label=label or chunk.meta.channel,
        unit=unit,
        start_s=chunk.start_s,
        band_low_hz=band_low_hz,
        band_high_hz=band_high_hz,
        demean=demean,
        peaks=peaks,
        window=window,
    )
