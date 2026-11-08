"""Q9：band_low 硬下限 2 Hz + 按窗长自动抬到「至少 10 个周期」（纯函数层）。

背景（``docs/reviews/2026-10-31-总体复查.md`` Q9）：``band_low`` 允许 0.1 Hz 时，
2 s 窗根本装不下 0.1 Hz 的周期，滤波等于只去直流，频域积分除以极小的 ω →
v_RMS 出现 556 mm/s 量级荒谬值。指标定义的默认值还能绕过窗口校验。

两条保护（推荐组合）：

1. 硬下限 ``BAND_LOW_FLOOR_HZ``（2 Hz）：保存时直接拒绝，中文原因；
2. 窗长兜底 ``band_low = max(band_low, 10 / window_s)``：算不出 10 个周期时自动抬高，
   并给出「已按窗长自动抬高到 X Hz」的中文提示（生效参数 / 计算 / 指纹同一口径）。
"""

from __future__ import annotations

import pytest

from lion_monitor.params import (
    BAND_LOW_FLOOR_HZ,
    clamp_band_low,
    effective_params,
    prepare_params,
    validate_params,
)

FS = 32000.0


def _params(low: float, window: float = 2.0) -> dict:
    return {
        "window_s": window,
        "step_s": window,
        "band_low": low,
        "band_high": 1000.0,
    }


def test_hard_floor_is_two_hz():
    assert BAND_LOW_FLOOR_HZ == 2.0


def test_schema_declares_hard_floor_as_min():
    """UI 的滑杆/数字框下限与校验共用一份声明。"""
    from lion_monitor.params import PARAM_SCHEMA

    item = next(row for row in PARAM_SCHEMA["v_RMS"] if row["key"] == "band_low")
    assert item["min"] == BAND_LOW_FLOOR_HZ


def test_validate_rejects_one_tenth_hz_with_chinese_reason():
    problems = validate_params(_params(0.1), sample_rate=FS)

    assert any("带通下限" in text and "2" in text for text in problems), problems


def test_validate_rejects_half_hz_below_hard_floor():
    """0.5 Hz 也在硬下限之下：保存直接拒绝（不是悄悄救活）。"""
    problems = validate_params(_params(0.5), sample_rate=FS)

    assert any("带通下限" in text for text in problems), problems


def test_validate_keeps_default_ten_hz_untouched():
    assert validate_params(_params(10.0), sample_rate=FS) == []
    assert validate_params(_params(10.0, window=10.0), sample_rate=FS) == []


def test_clamp_raises_low_to_ten_periods():
    raised, note = clamp_band_low({"band_low": 0.5, "band_high": 1000.0, "window_s": 2.0})

    assert raised["band_low"] == 5.0
    assert raised["band_high"] == 1000.0
    assert note is not None
    assert "自动抬高" in note and "5" in note


def test_clamp_leaves_values_that_already_fit():
    params = {"band_low": 10.0, "band_high": 1000.0, "window_s": 2.0}

    raised, note = clamp_band_low(params)

    assert raised["band_low"] == 10.0
    assert note is None


def test_clamp_needs_window_to_raise():
    """没有窗长信息（未导入数据 / 定义默认值只写带宽）就不猜，原样返回。"""
    raised, note = clamp_band_low({"band_low": 0.5, "band_high": 1000.0})

    assert raised["band_low"] == 0.5
    assert note is None


def test_prepare_rejects_below_hard_floor():
    params, problems, note = prepare_params(_params(0.1), sample_rate=FS)

    assert params["band_low"] == 0.1
    assert problems
    assert note is None


def test_prepare_raises_low_for_short_window_and_passes_validation():
    """band_low=2 Hz、窗长 2 s → 抬高到 5 Hz（10/2），校验通过并给出提示。"""
    params, problems, note = prepare_params(_params(2.0), sample_rate=FS)

    assert problems == []
    assert params["band_low"] == 5.0
    assert note is not None and "5" in note


def test_prepare_does_not_touch_valid_params():
    params, problems, note = prepare_params(_params(10.0), sample_rate=FS)

    assert problems == []
    assert params["band_low"] == 10.0
    assert note is None


def test_effective_params_raises_legacy_low_by_window():
    """历史/指标定义里已经存在的过小值：生效参数按窗长抬高（计算与指纹同一口径）。"""
    got = effective_params({"band_low": 0.5, "band_high": 1000.0, "window_s": 2.0}, FS)

    assert got["band_low"] == 5.0


def test_effective_params_never_below_hard_floor():
    got = effective_params({"band_low": 0.1, "band_high": 1000.0, "window_s": 20.0}, FS)

    assert got["band_low"] == 2.0


def test_effective_params_without_window_applies_hard_floor():
    """没有窗长信息时仍守住硬下限，历史脏值不会直接进计算。"""
    got = effective_params({"band_low": 0.5, "band_high": 1000.0}, FS)

    assert got["band_low"] == BAND_LOW_FLOOR_HZ


@pytest.mark.parametrize("window", [0.05, 0.5, 1.0, 2.0, 10.0])
def test_effective_band_always_covers_ten_periods(window):
    """不论窗长，生效带宽下限都保证「一个窗至少 10 个最低频周期」。"""
    got = effective_params({"band_low": 5.0, "band_high": 1000.0, "window_s": window}, FS)

    assert got["band_low"] * window >= 10.0 - 1e-9
