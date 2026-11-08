"""参数体系纯函数：默认值、逐层合并、参数指纹。"""

from __future__ import annotations

from lion_monitor import constants
from lion_monitor.params import PARAM_DEFAULTS, default_params, merge_params, params_hash


def test_default_params_for_builtin_codes():
    assert default_params("v_RMS") == {
        "window_s": 2.0,
        "step_s": 2.0,
        "band_low": 10.0,
        "band_high": 1000.0,
        # 复查 v2 问题 5：默认窗型改 rect（hanning 会把 10 Hz 带边缘幅值削掉 10.31%）
        "window_fn": "rect",
        "detrend": True,
    }
    # a_RMS 自 2026-11-02 起也带通（马哥要求），a_Peak 仍是纯时域峰值
    assert default_params("a_RMS") == {
        "window_s": 2.0,
        "step_s": 2.0,
        "band_low": 10.0,
        "band_high": 1000.0,
        "detrend": True,
    }
    assert default_params("a_Peak") == {"window_s": 2.0, "step_s": 2.0, "detrend": True}


def test_default_params_returns_copy():
    """改返回值不污染全局默认（否则一处改动会串到所有指标）。"""
    got = default_params("v_RMS")
    got["band_high"] = 12345.0
    assert PARAM_DEFAULTS["v_RMS"]["band_high"] == constants.VELOCITY_BAND[1]


def test_unknown_code_has_empty_defaults():
    assert default_params("不存在的指标") == {}


def test_merge_params_later_layers_win_and_none_ignored():
    system = {"a": 1, "b": 2}
    definition = {"b": 3}
    override = {"c": 4}
    assert merge_params(system, definition, None, override) == {"a": 1, "b": 3, "c": 4}


def test_merge_params_does_not_mutate_inputs():
    system = {"a": 1}
    merge_params(system, {"a": 2})
    assert system == {"a": 1}


def test_params_hash_is_stable_across_key_order():
    assert params_hash({"b": 2, "a": 1}) == params_hash({"a": 1, "b": 2})


def test_params_hash_truncated_and_changes_with_value():
    first = params_hash({"band_high": 1000.0})
    second = params_hash({"band_high": 2000.0})
    assert len(first) == 12 and first.isalnum()
    assert first != second


def test_params_hash_empty_is_blank():
    """没有参数快照时写空串（历史行与新行可区分）。"""
    assert params_hash(None) == ""
    assert params_hash({}) == ""
