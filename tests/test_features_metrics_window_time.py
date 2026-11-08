"""S4W-2：时域指标（a_Peak / a_RMS）与频域同口径按 ``window_s`` / ``step_s`` 切窗逐点。

口径与 S4W-1 完全一致（同一把 ``window_plan``，不另写一份切窗逻辑）：

- 2 s 窗 / 2 s 步长不重叠，20 s 记录 → 10 个点，时间偏移取**窗中心** 1,3,…,19 s
- 末段不足一窗丢弃；**整段短于一窗时一个值都不出**（G12：不足一窗直接判无效）
- 逐窗先按 ``detrend`` 各自去直流，再算 ``a_Peak = max|x|`` / ``a_RMS = sqrt(mean(x²))``
- ``window_fn`` 只属于频域（v_RMS 的 schema 项）：时域指标不消费窗函数
"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor.features.metrics import compute_metric_series, window_plan
from lion_monitor.params import default_params

FS = 32000.0


def _signal(seconds: float = 20.0, freq: float = 120.0, seed: int = 20261102) -> np.ndarray:
    """多频叠加 + 固定种子噪声（每个 2 s 窗的峰值/RMS 量级稳定，便于逐窗对比）。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(FS * seconds)) / FS
    return (
        0.8 * np.sin(2 * np.pi * freq * t)
        + 0.2 * np.sin(2 * np.pi * 300 * t)
        + 0.02 * rng.standard_normal(t.size)
    )


def _params(**overrides) -> dict:
    return {**default_params("a_Peak"), **overrides}


def _manual(segment: np.ndarray) -> tuple[float, float]:
    """手工算一段（去直流后）的峰值与 RMS——测试内部独立实现，不复用被测函数。"""
    x = segment - segment.mean()
    return float(np.max(np.abs(x))), float(np.sqrt(np.mean(x**2)))


def test_20s_2s_window_gives_ten_a_peak_and_a_rms_points_at_window_centers():
    """验收 1（上半）：20 s / 2 s 窗 → a_Peak 与 a_RMS 各 10 个点，偏移 1,3,…,19 s。"""
    series = compute_metric_series(
        _signal(20.0), FS, params=_params(window_s=2.0, step_s=2.0), codes=["a_Peak", "a_RMS"]
    )
    assert set(series) == {"a_Peak", "a_RMS"}
    for code in ("a_Peak", "a_RMS"):
        assert len(series[code]) == 10
        assert [round(offset, 6) for offset, _value in series[code]] == [
            float(i) for i in range(1, 20, 2)
        ]
        assert all(np.isfinite(value) and value > 0 for _offset, value in series[code])


def _band_rms(segment: np.ndarray, low: float = 10.0, high: float = 1000.0) -> float:
    """手工带通 RMS：去直流 → 频域置零 → irfft → 有效值（与 a_RMS 同口径）。"""
    x = segment - segment.mean()
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / FS)
    spectrum[(freqs < low) | (freqs > high)] = 0.0
    return float(np.sqrt(np.mean(np.fft.irfft(spectrum, n=x.size) ** 2)))


def test_a_rms_per_window_matches_manual_segment_rms_within_1e_9():
    """验收 1（下半）：第 k 个 a_RMS 与手工对第 k 段带通后算的 RMS 相对误差 < 1e-9。"""
    data = _signal(20.0)
    series = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0), codes=["a_RMS"]
    )["a_RMS"]
    per_window = int(round(2.0 * FS))
    for index, (_offset, value) in enumerate(series):
        segment = data[index * per_window : (index + 1) * per_window]
        manual_rms = _band_rms(segment)
        assert abs(value - manual_rms) <= 1e-9 * abs(manual_rms), (index, value, manual_rms)


def test_a_peak_per_window_matches_manual_segment_peak():
    """a_Peak 同一口径：逐窗峰值 = 该段去直流后的 max|x|（窗函数不参与）。"""
    data = _signal(4.0, seed=7)
    series = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0), codes=["a_Peak"]
    )["a_Peak"]
    per_window = int(round(2.0 * FS))
    assert len(series) == 2
    for index, (_offset, value) in enumerate(series):
        segment = data[index * per_window : (index + 1) * per_window]
        manual_peak, _rms = _manual(segment)
        assert value == pytest.approx(manual_peak, rel=1e-12)


def test_window_plan_is_shared_with_frequency_metrics():
    """切窗工具复用 S4W-1 的 ``window_plan``：5 s 窗 / 2 s 步长 → 8 个点（末段丢弃）。"""
    params = _params(window_s=5.0, step_s=2.0)
    assert window_plan(int(FS * 20), FS, params) == window_plan(
        int(FS * 20), FS, {**default_params("v_RMS"), "window_s": 5.0, "step_s": 2.0}
    )
    series = compute_metric_series(_signal(20.0), FS, params=params, codes=["a_Peak", "a_RMS"])
    assert [len(series[code]) for code in ("a_Peak", "a_RMS")] == [8, 8]
    assert [round(offset, 6) for offset, _value in series["a_RMS"]] == [
        2.5,
        4.5,
        6.5,
        8.5,
        10.5,
        12.5,
        14.5,
        16.5,
    ]


def test_short_record_below_one_window_yields_no_points():
    """G12：记录短于一窗 → 时域指标也不出值（与频域同一口径，不再兜底算 1 个窗）。"""
    series = compute_metric_series(
        _signal(1.0), FS, params=_params(window_s=2.0, step_s=2.0), codes=["a_Peak", "a_RMS"]
    )
    for code in ("a_Peak", "a_RMS"):
        assert series[code] == [], code


def test_time_domain_metrics_ignore_window_fn_but_v_rms_does_not():
    """window_fn 是频域参数：时域指标逐窗值不随 hanning/rect 变化，v_RMS 会变。"""
    data = _signal(4.0, seed=11)
    hann = compute_metric_series(
        data,
        FS,
        params=_params(window_s=2.0, step_s=2.0, window_fn="hanning"),
        codes=["a_Peak", "a_RMS", "v_RMS"],
    )
    rect = compute_metric_series(
        data,
        FS,
        params=_params(window_s=2.0, step_s=2.0, window_fn="rect"),
        codes=["a_Peak", "a_RMS", "v_RMS"],
    )
    assert hann["a_Peak"] == rect["a_Peak"]
    assert hann["a_RMS"] == rect["a_RMS"]
    assert hann["v_RMS"][0][1] != pytest.approx(rect["v_RMS"][0][1], rel=1e-6)


def test_detrend_off_keeps_dc_offset_in_peak_metric():
    """去直流关闭时峰值把直流分量算进去（a_RMS 走带通，直流在带外被砍）。"""
    data = _signal(2.0, seed=3) + 5.0
    on = compute_metric_series(
        data, FS, params=_params(window_s=2.0, detrend=True), codes=["a_Peak"]
    )["a_Peak"]
    off = compute_metric_series(
        data, FS, params=_params(window_s=2.0, detrend=False), codes=["a_Peak"]
    )["a_Peak"]
    assert off[0][1] > on[0][1] * 5  # 5 g 直流把峰值顶到 5 以上
