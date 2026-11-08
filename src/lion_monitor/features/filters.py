"""带通滤波（频域）：``rfft`` → 带外频点置零 → ``irfft``。

为什么用频域而不是巴特沃斯（马哥 2026-11-02 定 S4D，2026-11-03 推广到包络谱）：

- **零相位**：置零后逆变换没有 IIR 相位失真，也不需要 ``filtfilt`` 正反两遍；
- **没有「阶数」这个设置项**：阶数只是 IIR 逼近频响陡度的手段，频域置零天然是砖墙；
- **没有暂态**：IIR 起振会污染段首尾，频域置零不会；
- 返回长度与输入一致，调用方不用裁边。

指标层（``features.metrics``）与包络谱（``features.envelope``）共用这一份实现 ——
不要各写一份，口径会走偏。
"""

from __future__ import annotations

import numpy as np

__all__ = ["bandpass_freq"]


def bandpass_freq(
    samples: np.ndarray,
    sample_rate_hz: float,
    low_hz: float,
    high_hz: float,
) -> np.ndarray:
    """保留 ``low_hz ~ high_hz``（含端点）的频域带通，返回 float64、长度不变。

    ``low=0`` 且 ``high >= fs/2`` 视为全频带，原样返回（不做无意义的往返变换）。
    """
    x = np.asarray(samples, dtype=np.float64).ravel()
    nyquist = float(sample_rate_hz) / 2.0
    if float(low_hz) <= 0.0 and float(high_hz) >= nyquist:
        return x.copy()

    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / float(sample_rate_hz))
    spectrum[(freqs < float(low_hz)) | (freqs > float(high_hz))] = 0.0
    return np.fft.irfft(spectrum, n=x.size)
