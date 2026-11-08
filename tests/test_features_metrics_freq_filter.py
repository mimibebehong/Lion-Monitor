"""S4D：带通改频域滤波（`rfft` → 带外置零 → `irfft`），不再用 butter + sosfiltfilt。

验收（任务书 ``docs/kanban/bodies/s4d_brief.md``）：

1. 145 Hz / 0.8 gpk / fs=32 kHz / 20 s → ``v_RMS`` 与理论值相对误差 < 2%；
2. 100 Hz + 500 Hz 双音过 10–300 Hz 带通 → 与原始 100 Hz 分量逐点一致、峰值时间位置相同
   （零相位、无延迟、无边缘暂态）；
3. 1200 Hz 单音过 10–1000 Hz 带通 → 残差 < -40 dB；
4. ``velocity_series`` 长度与输入一致、数值有限。

前两条里「与手工 rfft 置零逐位一致」是这次改造的硬口径：频域滤波是**循环卷积**，
不引入 IIR 的阶数与暂态（``butter`` 在 1200 Hz 处只压到 -14.6 dB，达不到 -40 dB）。
"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor.constants import G_TO_MM_S2
from lion_monitor.features import metrics
from lion_monitor.features.metrics import _band_filter, compute_metrics, velocity_series

FS = 32000.0


def _tone(freq: float, seconds: float = 1.0, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(FS * seconds)) / FS
    return amplitude * np.sin(2 * np.pi * freq * t)


def _manual_band_zeroing(x: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    """手工口径：rfft → 带外频点置零 → irfft（不依赖被测代码）。"""
    low, high = band
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / FS)
    spectrum[(freqs < low) | (freqs > high)] = 0.0
    return np.fft.irfft(spectrum, n=x.size)


def test_band_filter_matches_manual_rfft_band_zeroing():
    """``_band_filter`` 就是「rfft → 带外置零 → irfft」：与手工实现逐位一致。"""
    t = np.arange(int(FS)) / FS
    x = 0.8 * np.sin(2 * np.pi * 120 * t) + 0.3 * np.sin(2 * np.pi * 3000 * t)

    got = _band_filter(x, FS, (10.0, 1000.0))
    want = _manual_band_zeroing(x, (10.0, 1000.0))

    assert got is not None
    assert got.shape == x.shape
    assert got == pytest.approx(want, rel=1e-12, abs=1e-15)


def test_metrics_module_no_longer_uses_iir_filter_design():
    """改造后计算层不应再引入 ``butter`` / ``sosfiltfilt``（阶数设置随之消失）。"""
    assert not hasattr(metrics, "butter")
    assert not hasattr(metrics, "sosfiltfilt")


def test_filter_is_zero_phase_and_keeps_the_in_band_tone_intact():
    """验收 2：100 Hz + 500 Hz 双音 → 10–300 Hz 带通后就是原 100 Hz 分量（无延迟、无相位偏移）。"""
    ref = _tone(100.0)
    x = ref + 0.5 * _tone(500.0)

    filtered = _band_filter(x, FS, (10.0, 300.0))

    assert filtered is not None
    # 两个频率在 1 s 内都是整数个周期 → 频域置零后应逐点还原成 100 Hz 分量
    assert np.max(np.abs(filtered - ref)) < 1e-6
    # 互相关峰值落在 0 位移 = 样本级零延迟
    lag = int(np.argmax(np.correlate(filtered, ref, mode="full"))) - (ref.size - 1)
    assert lag == 0
    # 100 Hz 分量的峰值样本（t = 2.5 ms）在滤波后仍是峰值所在样本
    peak = int(np.argmax(np.abs(ref)))
    window = slice(peak - 5, peak + 6)
    assert peak - 5 + int(np.argmax(np.abs(filtered[window]))) == peak


def test_out_of_band_tone_suppressed_below_minus_40_db():
    """验收 3：1200 Hz 单音过 10–1000 Hz 带通，残差 < -40 dB。"""
    x = _tone(1200.0)

    filtered = _band_filter(x, FS, (10.0, 1000.0))

    assert filtered is not None
    residual_db = 20 * np.log10(np.sqrt(np.mean(filtered**2)) / np.sqrt(np.mean(x**2)))
    assert residual_db < -40.0, f"带外残留 {residual_db:.2f} dB，未压到 -40 dB 以下"


def test_v_rms_of_145hz_matches_theory():
    """验收 1：145 Hz / 0.8 gpk / 20 s → v_RMS 与 a_rms/ω 的理论值相对误差 < 2%。"""
    x = _tone(145.0, seconds=20.0, amplitude=0.8)
    theory = (0.8 / np.sqrt(2)) * G_TO_MM_S2 / (2 * np.pi * 145.0)

    value = compute_metrics(x, FS, codes=["v_RMS"])["v_RMS"]

    assert value == pytest.approx(theory, rel=2e-2)
    # 频域滤波 + 频域积分在整数周期上没有幅值损失，实测误差远小于验收线
    assert value == pytest.approx(theory, rel=1e-6)


def test_velocity_series_length_matches_input_and_is_finite():
    """验收 4：``velocity_series`` 不做边缘裁剪，长度与输入一致且全部有限。"""
    x = 0.6 * _tone(145.0, seconds=2.0) + 0.2 * _tone(320.0, seconds=2.0)

    velocity = velocity_series(x, FS)

    assert velocity.shape == x.shape
    assert np.all(np.isfinite(velocity))
    assert np.sqrt(np.mean(velocity**2)) > 0.0
