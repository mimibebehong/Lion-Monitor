"""S1：``compute_metrics`` 接参数 —— 带通参数化 + 「不传参数结果不变」回归。

回归基准值（金标准）是**改造前**实现跑出来的真实结果，硬编码在 ``GOLDEN_*`` 里：
改造后不传 ``params`` 时结果必须逐位一致（相对误差 1e-12），这是 S1 的验收门槛。

S4D（2026-11-02）把带通从 ``butter(4, …) + sosfiltfilt`` 换成**频域滤波**（``rfft`` →
带外频点置零 → ``irfft``），带通本身不再有 IIR 增益/暂态误差，所以 ``v_RMS`` 三个金标准
按新口径重算（``a_Peak`` / ``a_RMS`` 不走滤波器，值不变）：

- ``GOLDEN_MIXED`` 10.405547108507731 → 10.405592400426606（差 4.3e-5，谐波与噪声能量重新分配）
- ``GOLDEN_SHORT`` 9.203996635063536 → 9.196965626918837（差 -0.08%，新值 = 60 Hz 理论值
  ``0.5/√2·9806.65/(2π·60)``，butter 在 10 Hz 下边沿略削了通带增益）
- ``GOLDEN_LOW_FS`` 0.5605509802906602 → 9.19696562691884（**旧值本身是 bug**：fs=4 kHz 时
  10 Hz 下边沿归一化到 0.005×Nyquist，4 阶 IIR 在 2000 点短记录上被暂态吃掉约 24 dB 增益；
  频域置零给回理论值 9.1970）
"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor import constants
from lion_monitor.features.metrics import compute_metrics
from lion_monitor.params import default_params

FS = 32000.0

#: 多频叠加 + 固定种子噪声（2 s）
GOLDEN_MIXED = {
    "a_Peak": 1.1918753775097566,
    # 2026-11-02 起 a_RMS 也走带通（10–1000 Hz）：3000 Hz 分量被砍，值从 0.6054623 降到 0.6041261
    "a_RMS": 0.6041260820831751,
    "v_RMS": 10.405592400426606,
}
#: 0.5 s 纯 60 Hz 正弦，fs=32 kHz
GOLDEN_SHORT = {"a_Peak": 0.5, "a_RMS": 0.3535533905932738, "v_RMS": 9.196965626918837}
#: 同上信号，fs=4 kHz（带通上限被奈奎斯特裁掉）
GOLDEN_LOW_FS = {"a_Peak": 0.5, "a_RMS": 0.3535533905932738, "v_RMS": 9.19696562691884}


def _mixed() -> np.ndarray:
    rng = np.random.default_rng(20261031)
    t = np.arange(int(FS * 2)) / FS
    return (
        0.8 * np.sin(2 * np.pi * 120 * t)
        + 0.3 * np.sin(2 * np.pi * 45 * t)
        + 0.05 * np.sin(2 * np.pi * 3000 * t)
        + 0.02 * rng.standard_normal(t.size)
    )


def _sine(freq: float, seconds: float = 2.0, fs: float = FS, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(fs * seconds)) / fs
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_default_call_matches_pre_change_golden_values():
    """不传 params：三个指标与改造前完全一致。"""
    values = compute_metrics(_mixed(), FS)
    assert values.keys() == GOLDEN_MIXED.keys()
    for code, expected in GOLDEN_MIXED.items():
        assert values[code] == pytest.approx(expected, rel=1e-12), code


def test_default_call_short_record_matches_golden_values():
    x = _sine(60.0, seconds=0.5, amplitude=0.5)
    values = compute_metrics(x, FS)
    for code, expected in GOLDEN_SHORT.items():
        assert values[code] == pytest.approx(expected, rel=1e-12), code


def test_default_call_low_sample_rate_matches_golden_values():
    """fs=4 kHz 的 0.5 s / 60 Hz 信号：v_RMS 与 32 kHz 同值（都是理论值 9.1970）。

    改造前这条金标准是 0.5606，但那个信号是**按 32 kHz 采样生成、按 4 kHz 计算**的：
    60 Hz 落到 7.5 Hz（带通下限 10 Hz 以下），旧值只是 butter 泄漏出来的残差。
    S4D 的带外硬截断把这点泄漏清零，于是改成按 4 kHz 采样生成同一物理信号，
    再与理论值 / 32 kHz 结果对照（0.95×Nyquist 夹紧本身由
    ``test_band_high_clamped_by_nyquist`` 覆盖）。
    """
    x = _sine(60.0, seconds=0.5, fs=4000.0, amplitude=0.5)
    values = compute_metrics(x, 4000.0)
    for code, expected in GOLDEN_LOW_FS.items():
        assert values[code] == pytest.approx(expected, rel=1e-12), code
    high_rate = compute_metrics(_sine(60.0, seconds=0.5, amplitude=0.5), FS)
    assert values["v_RMS"] == pytest.approx(high_rate["v_RMS"], rel=1e-9)


def test_explicit_default_params_equal_no_params():
    """显式传默认参数 = 不传参数（默认值就是从同一个常量派生的）。"""
    x = _mixed()
    baseline = compute_metrics(x, FS)
    explicit = compute_metrics(x, FS, params=default_params("v_RMS"))
    assert explicit == pytest.approx(baseline, rel=0)


def test_band_params_take_effect_on_v_rms():
    """把带通下限提到 100 Hz：45 Hz 成分被排除，只剩 120 Hz。"""
    x = _mixed()
    wide = compute_metrics(x, FS, params={"band_low": 10.0, "band_high": 1000.0})["v_RMS"]
    narrow = compute_metrics(x, FS, params={"band_low": 100.0, "band_high": 1000.0})["v_RMS"]
    assert narrow < wide * 0.85


def test_band_high_cut_removes_high_frequency_component():
    """带通上限压到 200 Hz：3 kHz 噪声被排除，值明显下降。"""
    x = _mixed()
    wide = compute_metrics(x, FS, params={"band_low": 10.0, "band_high": 1000.0})["v_RMS"]
    low = compute_metrics(x, FS, params={"band_low": 10.0, "band_high": 200.0})["v_RMS"]
    assert low < wide


def test_band_above_signal_is_near_zero():
    """带通完全避开信号频率（500–900 Hz）→ 速度 RMS 接近 0。"""
    values = compute_metrics(_sine(120.0), FS, params={"band_low": 500.0, "band_high": 900.0})
    assert values["v_RMS"] < 0.5


def test_band_high_clamped_by_nyquist():
    """带通上限超过奈奎斯特：裁到 fs/2×0.95，不抛异常。"""
    values = compute_metrics(
        _sine(60.0, fs=4000.0), 4000.0, params={"band_low": 100.0, "band_high": 5000.0}
    )
    assert values["v_RMS"] > 0.0


def test_detrend_false_keeps_dc_offset():
    """去直流开关：False 时直流偏置进入 a_Peak；a_RMS 走带通，直流本来就在带外。"""
    x = _sine(120.0, amplitude=0.1) + 2.0
    on = compute_metrics(x, FS, params={"detrend": True})
    off = compute_metrics(x, FS, params={"detrend": False})
    assert off["a_Peak"] > on["a_Peak"] * 5
    assert off["a_RMS"] == pytest.approx(on["a_RMS"], rel=1e-9)


def test_params_and_codes_together():
    """params 与 codes 同时给：只算指定指标，其余不出现。"""
    values = compute_metrics(_mixed(), FS, params={"band_low": 100.0}, codes=["v_RMS"])
    assert list(values) == ["v_RMS"]


def test_custom_band_does_not_leak_into_next_default_call():
    """自定义带通不污染后续默认调用。"""
    compute_metrics(_mixed(), FS, params={"band_low": 500.0, "band_high": 900.0})
    assert compute_metrics(_mixed(), FS)["v_RMS"] == pytest.approx(GOLDEN_MIXED["v_RMS"], rel=1e-12)


def test_velocity_band_constant_still_default_source():
    """VELOCITY_BAND 降级为默认值，但值不变仍是单一来源。"""
    assert constants.VELOCITY_BAND == (10.0, 1000.0)
    assert default_params("v_RMS")["band_low"] == constants.VELOCITY_BAND[0]
    assert default_params("v_RMS")["band_high"] == constants.VELOCITY_BAND[1]
