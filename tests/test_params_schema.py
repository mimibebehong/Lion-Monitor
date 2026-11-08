"""S2：参数 schema 声明、默认值派生、校验、覆盖 diff、转速门控（纯函数层）。"""

from __future__ import annotations

import pytest

from lion_monitor import constants
from lion_monitor.params import (
    BANDWIDTH_KEYS,
    PARAM_SCHEMA,
    default_params,
    diff_params,
    merge_params,
    needs_speed,
    resolve_rpm,
    schema_defaults,
    validate_params,
)


def _item(code: str, key: str) -> dict:
    return next(item for item in PARAM_SCHEMA[code] if item["key"] == key)


def test_builtin_codes_all_declare_schema():
    assert set(PARAM_SCHEMA) == set(constants.METRIC_UNITS)


def test_v_rms_schema_matches_agreed_defaults():
    """马哥 2026-10-31 已定：带通 10–1000 Hz、时长 2 s、步长 2 s（不重叠）。"""
    schema = PARAM_SCHEMA["v_RMS"]
    assert [item["key"] for item in schema] == [
        "window_s",
        "step_s",
        "band_low",
        "band_high",
        "window_fn",
        "detrend",
    ]
    assert _item("v_RMS", "window_s")["default"] == 2.0
    assert _item("v_RMS", "step_s")["default"] == 2.0
    assert _item("v_RMS", "band_low")["default"] == constants.VELOCITY_BAND[0]
    assert _item("v_RMS", "band_high")["default"] == constants.VELOCITY_BAND[1]
    # 复查 v2 问题 5：v_RMS 默认窗型改 rect —— 2 s 窗下 hanning 主瓣 2 Hz 宽，
    # 10 Hz 分量正好压在 band_low 上，主瓣 8–10 Hz 那半被带通切掉、幅值低估 10.31%。
    assert _item("v_RMS", "window_fn")["default"] == "rect"
    assert _item("v_RMS", "detrend")["default"] is True


def test_schema_groups_split_basic_and_advanced():
    """基本参数常改、高级参数（带宽/算法）默认折叠 —— 分组在 schema 里声明。"""
    groups = {item["key"]: item["group"] for item in PARAM_SCHEMA["v_RMS"]}
    assert groups["window_s"] == "basic"
    assert groups["step_s"] == "basic"
    for key in ("band_low", "band_high", "window_fn", "detrend"):
        assert groups[key] == "advanced"
    assert set(BANDWIDTH_KEYS) == {"band_low", "band_high"}


def test_every_schema_item_is_renderable():
    """UI 全靠声明渲染：key/label/group/type/default 缺一不可，type 必须是支持的类型。"""
    for code, schema in PARAM_SCHEMA.items():
        for item in schema:
            assert {"key", "label", "group", "type", "default"} <= set(item), (code, item)
            assert item["group"] in {"basic", "advanced"}, (code, item)
            assert item["type"] in {"float", "int", "bool", "enum"}, (code, item)
            if item["type"] == "enum":
                assert item["default"] in item["options"], (code, item)
            if item["type"] in {"float", "int"}:
                assert isinstance(item["default"], int | float), (code, item)


def test_default_params_derive_from_schema():
    """硬默认由 schema 派生，避免两处各写一份（改 schema 默认即改硬默认）。"""
    for code, schema in PARAM_SCHEMA.items():
        assert default_params(code) == schema_defaults(schema)


def test_schema_defaults_returns_plain_dict():
    assert schema_defaults(PARAM_SCHEMA["a_Peak"]) == {
        "window_s": 2.0,
        "step_s": 2.0,
        "detrend": True,
    }
    assert schema_defaults([]) == {}


def test_time_domain_schema_declares_window_and_step():
    """S4W-2：a_Peak / a_RMS 与频域同口径声明窗参数，默认 2 s / 2 s（照抄已定值）。"""
    for code in ("a_Peak", "a_RMS"):
        assert [item["key"] for item in PARAM_SCHEMA[code]][:2] == ["window_s", "step_s"], code
        assert _item(code, "window_s")["default"] == 2.0, code
        assert _item(code, "step_s")["default"] == 2.0, code
        assert _item(code, "window_s")["group"] == "basic", code
        assert _item(code, "step_s")["group"] == "basic", code
    # 三个内置指标的窗口径一致（同一把切窗工具，默认值不许各写一份）
    for code in ("a_Peak", "a_RMS"):
        assert default_params(code)["window_s"] == default_params("v_RMS")["window_s"], code
        assert default_params(code)["step_s"] == default_params("v_RMS")["step_s"], code


def test_validate_accepts_agreed_defaults():
    assert validate_params(default_params("v_RMS"), sample_rate=32000.0) == []
    assert validate_params(default_params("a_Peak"), sample_rate=32000.0) == []


def test_validate_rejects_band_high_above_nyquist():
    problems = validate_params({"band_low": 10.0, "band_high": 20000.0}, sample_rate=32000.0)
    assert any("奈奎斯特" in text for text in problems), problems


def test_validate_accepts_band_high_at_nyquist():
    assert validate_params({"band_low": 10.0, "band_high": 16000.0}, sample_rate=32000.0) == []


def test_validate_rejects_window_shorter_than_ten_periods():
    """window_s ≥ 10 / band_low：下限 10 Hz → 至少 1 s。"""
    problems = validate_params(
        {"band_low": 10.0, "band_high": 1000.0, "window_s": 0.5, "step_s": 0.5},
        sample_rate=32000.0,
    )
    assert any("计算时长" in text for text in problems), problems
    # 下限抬到 100 Hz → 0.5 s 就够了
    assert (
        validate_params(
            {"band_low": 100.0, "band_high": 1000.0, "window_s": 0.5, "step_s": 0.5},
            sample_rate=32000.0,
        )
        == []
    )


def test_validate_rejects_step_longer_than_window():
    problems = validate_params(
        {"band_low": 10.0, "band_high": 1000.0, "window_s": 2.0, "step_s": 3.0},
        sample_rate=32000.0,
    )
    assert any("计算步长" in text for text in problems), problems


def test_validate_accepts_step_equal_to_window():
    """步长 = 时长 = 不重叠（已定默认）。"""
    assert (
        validate_params(
            {"band_low": 10.0, "band_high": 1000.0, "window_s": 2.0, "step_s": 2.0},
            sample_rate=32000.0,
        )
        == []
    )


def test_validate_rejects_inverted_band():
    problems = validate_params({"band_low": 900.0, "band_high": 100.0}, sample_rate=32000.0)
    assert any("带通" in text for text in problems), problems


def test_validate_honours_schema_min_max():
    problems = validate_params({"band_low": 0.01, "band_high": 1000.0}, sample_rate=32000.0)
    assert any("下限" in text for text in problems), problems


def test_validate_rejects_unknown_enum_option():
    problems = validate_params({"window_fn": "kaiser_2"}, sample_rate=32000.0)
    assert any("窗函数" in text for text in problems), problems


def test_validate_skips_fs_rule_when_rate_unknown():
    """还没导入数据（采样率未知）时只校验与采样率无关的规则。"""
    assert validate_params({"band_low": 10.0, "band_high": 20000.0}, sample_rate=None) == []
    assert validate_params({"band_low": 10.0, "band_high": 1000.0, "step_s": 5.0}) != []


def test_validate_ignores_keys_outside_schema():
    """节点覆盖里带的历史字段不阻塞保存。"""
    assert validate_params({"detrend": True, "已废弃字段": 1}, sample_rate=32000.0) == []


def test_diff_params_keeps_only_changed_items():
    base = default_params("v_RMS")
    assert diff_params({**base, "band_high": 2000.0}, base) == {"band_high": 2000.0}


def test_diff_params_is_empty_when_equal():
    base = default_params("v_RMS")
    assert diff_params(dict(base), base) == {}
    # 1000 与 1000.0 视为同一个值
    assert diff_params({**base, "band_high": 1000}, base) == {}


def test_diff_params_drops_items_missing_from_current():
    """覆盖项被删掉 → diff 里也不该留。"""
    base = {"band_low": 10.0, "band_high": 1000.0}
    assert diff_params({"band_high": 1500.0}, base) == {"band_high": 1500.0}


def test_needs_speed_reads_scaling_declaration():
    speed_schema = [
        {"key": "f0_hz", "label": "特征频率", "group": "basic", "type": "float", "default": 145.0,
         "scaling": "speed"},
    ]  # fmt: skip
    fixed_schema = [
        {"key": "f0_hz", "label": "线频", "group": "basic", "type": "float", "default": 100.0,
         "scaling": "fixed"},
    ]  # fmt: skip
    assert needs_speed(speed_schema) is True
    assert needs_speed(fixed_schema) is False
    assert needs_speed(PARAM_SCHEMA["v_RMS"]) is False
    assert needs_speed([]) is False


def test_resolve_rpm_priority():
    """节点手填 > 批次实测 > 对象额定；都没有就是 None，绝不猜。"""
    assert resolve_rpm(node_rpm=1480.0, measured_rpm=1500.0, rated_rpm=1450.0) == (1480.0, "node")
    assert resolve_rpm(node_rpm=None, measured_rpm=1500.0, rated_rpm=1450.0) == (1500.0, "measured")
    assert resolve_rpm(node_rpm=None, measured_rpm=None, rated_rpm=1450.0) == (1450.0, "rated")
    assert resolve_rpm(node_rpm=None, measured_rpm=None, rated_rpm=None) == (None, None)


def test_resolve_rpm_ignores_non_positive_values():
    """0/负数转速是脏数据，按缺失处理。"""
    assert resolve_rpm(node_rpm=0.0, measured_rpm=-5.0, rated_rpm=None) == (None, None)


def test_merge_params_used_for_effective_chain():
    """生效参数 = 硬默认 < 定义默认 < 节点覆盖。"""
    base = default_params("v_RMS")
    definition = {"band_high": 1500.0}
    override = {"band_high": 2000.0, "window_s": 4.0}
    effective = merge_params(base, definition, override)
    assert effective["band_high"] == 2000.0
    assert effective["window_s"] == 4.0
    assert effective["band_low"] == base["band_low"]


def test_locked_semantics_is_full_snapshot():
    """固化（params_locked=1）：params_json 就是全量生效值，不再与定义合并。"""
    base = default_params("v_RMS")
    frozen = {**base, "band_high": 2000.0}
    assert merge_params(frozen) == frozen
    assert diff_params(frozen, frozen) == {}


@pytest.mark.parametrize("code", list(constants.METRIC_UNITS))
def test_default_params_valid_for_every_builtin(code):
    assert validate_params(default_params(code), sample_rate=25600.0) == []
