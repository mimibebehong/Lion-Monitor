"""包络谱带通改频域滤波、复用已有算法、删掉「滤波器阶数」（2026-11-03）。"""

from __future__ import annotations

import inspect

import numpy as np
import pytest

from lion_monitor.features import envelope, metrics
from lion_monitor.features.envelope import bandpass_filter, envelope_spectrum_from_samples
from lion_monitor.features.filters import bandpass_freq

FS = 32000.0


def _two_tone() -> np.ndarray:
    n = int(FS * 4.0)
    t = np.arange(n) / FS
    return 0.1 * np.sin(2 * np.pi * 500.0 * t) + 0.5 * np.sin(2 * np.pi * 5000.0 * t)


def _am() -> np.ndarray:
    n = int(FS * 4.0)
    t = np.arange(n) / FS
    return (1.0 + 0.5 * np.cos(2 * np.pi * 25.0 * t)) * np.cos(2 * np.pi * 5000.0 * t)


def test_envelope_bandpass_has_no_order_parameter():
    """「滤波器阶数」选项删掉：频域滤波没有阶数这个概念。"""
    params = inspect.signature(bandpass_filter).parameters
    assert "order" not in params, "带通不应再有阶数参数（频域滤波无阶数）"
    assert not hasattr(envelope, "FILTER_ORDER_MIN")
    assert not hasattr(envelope, "FILTER_ORDER_MAX")
    assert not hasattr(envelope, "DEFAULT_FILTER_ORDER")


def test_envelope_bandpass_reuses_shared_frequency_domain_filter():
    """包络带通复用的就是已有的频域带通（与指标口径同一实现）。"""
    x = _two_tone()
    assert np.allclose(bandpass_filter(x, FS, 4000.0, 6000.0), bandpass_freq(x, FS, 4000.0, 6000.0))


def test_metric_band_filter_shares_the_same_implementation():
    """指标层的 _band_filter 也走同一个频域带通，不各写一份。"""
    x = _two_tone()
    metric = metrics._band_filter(x, FS, (4000.0, 6000.0))
    assert metric is not None
    assert np.allclose(metric, bandpass_freq(x, FS, 4000.0, 6000.0))


def test_frequency_domain_bandpass_rejects_out_of_band_energy():
    """500 Hz 分量应被压到 ~0，5 kHz 保留（RMS ≈ 0.5/√2）。"""
    y = bandpass_filter(_two_tone(), FS, 4000.0, 6000.0)
    assert np.sqrt(np.mean(y**2)) == pytest.approx(0.5 / np.sqrt(2.0), rel=0.02)


def test_frequency_domain_bandpass_is_zero_phase():
    """频域置零是零相位：滤波不改变包络调制分量的相位（有延迟会直接体现在相位差上）。

    不能用 argmax 比峰值位置 —— 调制包络每 40 ms 重复一次，幅值相等，argmax 会落在任意一个周期。
    """
    x = _am()
    y = bandpass_filter(x, FS, 4000.0, 6000.0)

    def mod_phase(signal: np.ndarray) -> float:
        from scipy.signal import hilbert

        env = np.abs(hilbert(signal))
        spec = np.fft.rfft(env - env.mean())
        freqs = np.fft.rfftfreq(env.size, d=1.0 / FS)
        return float(np.angle(spec[int(np.argmin(np.abs(freqs - 25.0)))]))

    delta = abs(mod_phase(x) - mod_phase(y))
    delta = min(delta, 2 * np.pi - delta)
    assert delta < 0.02, f"调制相位偏移 {delta:.4f} rad，说明滤波引入了延迟"


def test_full_band_still_passes_through():
    x = _two_tone()
    assert np.allclose(bandpass_filter(x, FS, 0.0, FS / 2), x)


def test_invalid_edges_still_raise():
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, 6000.0, 2000.0)
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, -10.0, 2000.0)
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, 2000.0, FS)


def test_envelope_spectrum_still_finds_25hz_without_order():
    """去掉阶数后，AM 信号包络谱主峰仍应是 25 Hz。"""
    spectrum = envelope_spectrum_from_samples(_am(), FS, label="AM", unit="g")
    main = spectrum.main_peak()
    assert main is not None
    assert main.freq_hz == pytest.approx(25.0, abs=1.0)
