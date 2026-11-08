"""G12：不足一窗的样本直接判无效（不出值 + 给具体原因）。

马哥 2026-11-02 定：「不足指标计算窗口的数据直接判无效」。
S4W-1 / S4W-2 引入的「整段短于一窗时整段当 1 个窗」兜底撤销——
``n < window_s × fs`` 时该指标一个值都不出，原因文案写明点数 / 窗长 / 采样率 / 所需点数，
例如 ``样本 32 点，不足一窗（2 s @ 32000 Hz 需 64000 点）``。

边界：``n == window_s × fs`` 恰好一窗 → **有效**（1 个值）；``n <= 0`` → 无效。
"""

from __future__ import annotations

import numpy as np

from lion_monitor.features.metrics import (
    compute_metric_series,
    short_sample_reason,
    window_plan,
    window_samples,
)
from lion_monitor.params import default_params

FS = 32000.0


def _params(**overrides) -> dict:
    return {**default_params("v_RMS"), **overrides}


def _signal(points: int, freq: float = 100.0) -> np.ndarray:
    t = np.arange(points) / FS
    return np.sin(2 * np.pi * freq * t)


def test_window_plan_32_samples_has_no_window():
    """验收 1：32 点远不足 2 s 窗 → 一个窗都没有（不给 0 值兜底）。"""
    assert window_plan(32, FS, _params(window_s=2.0, step_s=2.0)) == []


def test_window_plan_one_second_is_below_two_second_window():
    """验收 2：1 s（32000 点）仍不足 2 s 窗 → 0 个窗。"""
    assert window_plan(int(FS), FS, _params(window_s=2.0, step_s=2.0)) == []


def test_window_plan_exactly_one_window_is_valid():
    """验收 3：恰好一窗（64000 点）→ 1 个窗（边界含等号）。"""
    assert window_plan(int(FS * 2), FS, _params(window_s=2.0, step_s=2.0)) == [(0, int(FS * 2))]


def test_window_plan_four_seconds_two_second_window_gives_two_windows():
    """验收 4：4 s、2 s 窗 / 2 s 步长 → 2 个窗。"""
    assert window_plan(int(FS * 4), FS, _params(window_s=2.0, step_s=2.0)) == [
        (0, int(FS * 2)),
        (int(FS * 2), int(FS * 4)),
    ]


def test_window_samples_follows_effective_window_s():
    """一窗所需点数 = round(window_s × fs)，按该指标生效参数算（不是写死 2 s）。"""
    assert window_samples(FS, _params(window_s=2.0)) == 64000
    assert window_samples(FS, _params(window_s=0.05)) == 1600
    assert window_samples(FS, None) == 64000


def test_short_sample_reason_names_points_window_and_required_samples():
    """验收 1 的文案：点数 / 窗长 / 采样率 / 所需点数都要写清。"""
    reason = short_sample_reason(32, FS, _params(window_s=2.0))
    assert reason == "样本 32 点，不足一窗（2 s @ 32000 Hz 需 64000 点）"
    for token in ("32 点", "不足一窗", "64000"):
        assert token in reason, reason


def test_short_sample_reason_empty_when_window_is_full():
    """够一窗（含恰好一窗）→ 空串（不报原因）。"""
    assert short_sample_reason(int(FS * 2), FS, _params(window_s=2.0)) == ""
    assert short_sample_reason(int(FS * 4), FS, _params(window_s=2.0)) == ""


def test_short_sample_reason_zero_samples_is_invalid():
    """0 点 / 负点数 → 无效并给原因。"""
    assert short_sample_reason(0, FS, _params()) == "采样点数为 0"
    assert short_sample_reason(-5, FS, _params()) == "采样点数为 0"


def test_compute_metric_series_32_samples_yields_no_values():
    """验收 1：32 点样本 → 三个指标都不出值。"""
    series = compute_metric_series(_signal(32), FS, params=_params(window_s=2.0))
    assert series == {"a_Peak": [], "a_RMS": [], "v_RMS": []}


def test_compute_metric_series_one_second_yields_no_values():
    """验收 2：32000 点（1 s，不足 2 s 窗）→ 0 个值。"""
    series = compute_metric_series(_signal(int(FS)), FS, params=_params(window_s=2.0))
    assert [len(points) for points in series.values()] == [0, 0, 0]


def test_compute_metric_series_exactly_one_window_yields_one_point():
    """验收 3：64000 点（恰好一窗）→ 每个指标 1 个值（偏移 = 窗中心 1.0 s）。"""
    series = compute_metric_series(_signal(int(FS * 2)), FS, params=_params(window_s=2.0))
    for code in ("a_Peak", "a_RMS", "v_RMS"):
        assert len(series[code]) == 1, code
        assert series[code][0][0] == 1.0


def test_compute_metric_series_four_seconds_yields_two_points():
    """验收 4：128000 点（4 s）→ 每个指标 2 个值（偏移 1.0 / 3.0）。"""
    series = compute_metric_series(_signal(int(FS * 4)), FS, params=_params(window_s=2.0))
    for code in ("a_Peak", "a_RMS", "v_RMS"):
        assert [round(offset, 6) for offset, _value in series[code]] == [1.0, 3.0], code
