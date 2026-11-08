"""S4W-1：频域指标按 ``window_s`` / ``step_s`` 切窗逐点。

验收口径（马哥已定）：2 s 窗 / 2 s 步长不重叠，末段不足一窗丢弃；
**整段短于一窗时一个值都不出**（G12，2026-11-02：不足一窗直接判无效，撤销 S4W-1 的
「整段当 1 个窗」兜底）。``computed_at`` 用**窗中心**相对记录起点的偏移。
"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor.features.metrics import compute_metric_series, window_plan
from lion_monitor.params import default_params, params_hash

FS = 32000.0


def _signal(seconds: float = 20.0, freq: float = 120.0, seed: int = 20261102) -> np.ndarray:
    """多频叠加 + 固定种子噪声（幅值量级稳定，便于窗口间对比）。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(FS * seconds)) / FS
    return (
        0.8 * np.sin(2 * np.pi * freq * t)
        + 0.2 * np.sin(2 * np.pi * 300 * t)
        + 0.02 * rng.standard_normal(t.size)
    )


def _params(**overrides) -> dict:
    return {**default_params("v_RMS"), **overrides}


def test_window_plan_20s_2s_window_2s_step_gives_ten_windows():
    """20 s 数据、2 s 窗、2 s 步长 → 正好 10 个窗，首尾相接不重叠。"""
    plan = window_plan(int(FS * 20), FS, _params(window_s=2.0, step_s=2.0))
    assert len(plan) == 10
    assert plan[0] == (0, int(FS * 2))
    assert plan[1] == (int(FS * 2), int(FS * 4))
    assert plan[-1] == (int(FS * 18), int(FS * 20))


def test_window_plan_5s_window_2s_step_drops_trailing_partial_window():
    """20 s、5 s 窗、2 s 步长 → (20-5)//2+1 = 8 个窗；末段 19~20 s 不足一窗丢弃。"""
    plan = window_plan(int(FS * 20), FS, _params(window_s=5.0, step_s=2.0))
    assert len(plan) == 8
    assert plan[-1] == (int(FS * 14), int(FS * 19))


def test_window_plan_short_record_has_no_window():
    """G12：记录短于一窗 → 0 个窗（撤销 S4W-1「整段当 1 个窗」兜底，不再出 0 值）。"""
    assert window_plan(int(FS), FS, _params(window_s=2.0, step_s=2.0)) == []
    assert window_plan(32, FS, _params(window_s=2.0, step_s=2.0)) == []


def test_compute_metric_series_20s_2s_gives_ten_v_rms_points_at_window_centers():
    """验收 1：20 s / 2 s 窗 → v_RMS 恰好 10 个点，时间偏移 = 窗中心 1,3,…,19 s。"""
    series = compute_metric_series(
        _signal(20.0), FS, params=_params(window_s=2.0, step_s=2.0), codes=["v_RMS"]
    )["v_RMS"]
    assert len(series) == 10
    assert [round(offset, 6) for offset, _value in series] == [float(i) for i in range(1, 20, 2)]
    assert all(np.isfinite(value) and value > 0 for _offset, value in series)


def test_compute_metric_series_5s_window_2s_step_gives_eight_points():
    """验收 2：window_s=5、step_s=2 → 8 个点，偏移 2.5,4.5,…,16.5（末段丢弃）。"""
    series = compute_metric_series(
        _signal(20.0), FS, params=_params(window_s=5.0, step_s=2.0), codes=["v_RMS"]
    )["v_RMS"]
    assert len(series) == 8
    assert [round(offset, 6) for offset, _value in series] == [
        2.5,
        4.5,
        6.5,
        8.5,
        10.5,
        12.5,
        14.5,
        16.5,
    ]


def test_window_fn_hann_and_rect_give_different_values_and_fingerprints():
    """验收 3：window_fn=hanning 与 rect → 数值不同，参数指纹也不同。"""
    data = _signal(4.0, seed=7)
    hann = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0, window_fn="hanning"), codes=["v_RMS"]
    )["v_RMS"]
    rect = compute_metric_series(
        data, FS, params=_params(window_s=2.0, step_s=2.0, window_fn="rect"), codes=["v_RMS"]
    )["v_RMS"]
    assert len(hann) == len(rect) == 2
    assert hann[0][1] != pytest.approx(rect[0][1], rel=1e-6)
    assert params_hash(_params(window_fn="hanning"), code="v_RMS", sample_rate=FS) != params_hash(
        _params(window_fn="rect"), code="v_RMS", sample_rate=FS
    )


def test_rect_window_value_matches_manual_segment_compute():
    """rect 窗 = 不做窗：每个窗的值与手工对该段调用整段算法完全一致。"""
    data = _signal(4.0, seed=11)
    params = _params(window_s=2.0, step_s=2.0, window_fn="rect")
    series = compute_metric_series(data, FS, params=params, codes=["v_RMS"])["v_RMS"]
    per_window = int(FS * 2)
    for index, (_offset, value) in enumerate(series):
        segment = data[index * per_window : (index + 1) * per_window]
        manual = compute_metric_series(segment, FS, params=_params(window_s=2.0, window_fn="rect"))
        assert value == pytest.approx(manual["v_RMS"][0][1], rel=1e-12)


def test_time_domain_metrics_follow_the_same_window_plan():
    """S4W-2：时域指标不再整段 1 点——4 s / 2 s 窗 → a_Peak、a_RMS 各 2 点，偏移 1.0、3.0。"""
    series = compute_metric_series(
        _signal(4.0), FS, params=_params(window_s=2.0), codes=["a_Peak", "a_RMS"]
    )
    assert list(series) == ["a_Peak", "a_RMS"]
    for code in ("a_Peak", "a_RMS"):
        assert [round(offset, 6) for offset, _value in series[code]] == [1.0, 3.0]
        assert all(value > 0 for _offset, value in series[code])


def test_unknown_window_fn_raises_instead_of_silently_using_rect():
    """不认识的窗函数直接报错（保存层已拦，老库里漏进来的值不许静默当 rect）。"""
    with pytest.raises(ValueError, match="窗函数"):
        compute_metric_series(
            _signal(2.0), FS, params=_params(window_s=2.0, window_fn="kaiser_2"), codes=["v_RMS"]
        )


def test_same_window_params_same_fingerprint_different_window_different_fingerprint():
    """验收 4：同一窗参数 → 同一指纹；窗长/步长/窗函数任一不同 → 指纹不同。"""
    base = _params(window_s=2.0, step_s=2.0, window_fn="hanning")
    same = dict(reversed(list(base.items())))
    assert params_hash(base, code="v_RMS", sample_rate=FS) == params_hash(
        same, code="v_RMS", sample_rate=FS
    )
    for changed in (
        _params(window_s=5.0, step_s=2.0, window_fn="hanning"),
        _params(window_s=2.0, step_s=1.0, window_fn="hanning"),
        _params(window_s=2.0, step_s=2.0, window_fn="rect"),
    ):
        assert params_hash(changed, code="v_RMS", sample_rate=FS) != params_hash(
            base, code="v_RMS", sample_rate=FS
        )
