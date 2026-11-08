"""阈值规则单一来源：方向口径、保存校验、判级（S-1）。

口径（马哥 2026-10-31 拍板）：
- warn 是**级别更轻**，不是数值更小；
- ``upper``（越大越差）：warn < alert 才合法，值 >= warn → warn、值 >= alert → alert；
- ``lower``（越小越差）：warn > alert 才合法，值 <= warn → warn、值 <= alert → alert。
"""

from __future__ import annotations

import pytest

from lion_monitor import limits_rules


def test_upper_requires_warn_below_alert():
    limits_rules.validate_limit("upper", 2.8, 4.5)


def test_lower_requires_warn_above_alert():
    limits_rules.validate_limit("lower", 3.0, 1.0)


def test_upper_rejects_warn_not_below_alert_with_direction_message():
    with pytest.raises(limits_rules.LimitError) as err:
        limits_rules.validate_limit("upper", 4.5, 2.8)

    text = str(err.value)
    assert "上限" in text
    assert "warn < alert" in text
    assert "4.5" in text and "2.8" in text


def test_lower_rejects_warn_not_above_alert_with_direction_message():
    with pytest.raises(limits_rules.LimitError) as err:
        limits_rules.validate_limit("lower", 1.0, 3.0)

    text = str(err.value)
    assert "下限" in text
    assert "warn > alert" in text
    assert "1" in text and "3" in text


def test_validate_limit_accepts_store_markers():
    limits_rules.validate_limit(">", 2.8, 4.5)
    limits_rules.validate_limit("<", 3.0, 1.0)


def test_validate_limit_rejects_unknown_direction():
    with pytest.raises(limits_rules.LimitError, match="方向"):
        limits_rules.validate_limit("sideways", 1.0, 2.0)


def test_normalize_direction_maps_legacy_marker():
    assert limits_rules.normalize_direction("<") == "lower"
    assert limits_rules.normalize_direction(">") == "upper"
    assert limits_rules.normalize_direction("lower") == "lower"
    assert limits_rules.normalize_direction("upper") == "upper"
    # 读路径宽松：认不出的方向按上限口径处理，不炸页面
    assert limits_rules.normalize_direction("sideways") == "upper"


def test_marker_and_text_helpers():
    assert limits_rules.marker_of("lower") == "<"
    assert limits_rules.marker_of(">") == ">"
    assert limits_rules.text_of("upper") == "上限（越大越差）"
    assert limits_rules.text_of("<") == "下限（越小越差）"
    assert limits_rules.template_of("<") == "lower"


def test_grade_lower_direction():
    # 油压「越小越差」：warn=3（较轻）/ alert=1（更严重）
    assert limits_rules.grade(3.2, "lower", 3.0, 1.0) == "normal"
    assert limits_rules.grade(2.5, "lower", 3.0, 1.0) == "warn"
    assert limits_rules.grade(0.8, "lower", 3.0, 1.0) == "alert"


def test_grade_upper_direction():
    # 振动「越大越差」：warn=2.8 / alert=4.5
    assert limits_rules.grade(1.0, "upper", 2.8, 4.5) == "normal"
    assert limits_rules.grade(2.9, "upper", 2.8, 4.5) == "warn"
    assert limits_rules.grade(5.0, "upper", 2.8, 4.5) == "alert"


def test_grade_boundaries_hit_warn_and_alert():
    assert limits_rules.grade(2.8, "upper", 2.8, 4.5) == "warn"
    assert limits_rules.grade(4.5, "upper", 2.8, 4.5) == "alert"
    assert limits_rules.grade(3.0, "lower", 3.0, 1.0) == "warn"
    assert limits_rules.grade(1.0, "lower", 3.0, 1.0) == "alert"


def test_grade_accepts_store_markers():
    assert limits_rules.grade(2.5, "<", 3.0, 1.0) == "warn"


def test_problem_of_flags_legacy_row_without_changing_values():
    assert limits_rules.problem_of("upper", 2.8, 4.5) is None
    assert limits_rules.problem_of("lower", 3.0, 1.0) is None

    note = limits_rules.problem_of("lower", 0.2, 0.5)
    assert note is not None
    assert "下限" in note
    assert "warn > alert" in note
