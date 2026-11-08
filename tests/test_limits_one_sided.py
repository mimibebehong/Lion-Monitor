"""阈值可以只设一侧（马哥 2026-11-04：只设定 warn 或只设 alert）。

现状曾是「两列必须同时填」，单边只能靠填 0 绕过，而 0 又被当成真门槛
（值 5 就判预警）。新口径：**0 = 该档未设**，不算门槛。

| 配置 | 效果 |
|---|---|
| warn=9，alert 留空 | 值 ≥9 → 预警，永不告警 |
| warn 留空，alert=9 | 值 ≥9 → 告警，中间没有预警档 |
| 两侧都填 | 正常三级：健康 / 预警 / 告警 |
"""

from __future__ import annotations

import pytest

from lion_monitor.features import health
from lion_monitor.limits_rules import LimitError, grade, problem_of, validate_limit


def test_validate_allows_warn_only():
    validate_limit(">", 9.0, 0.0)  # 只设 warn


def test_validate_allows_alert_only():
    validate_limit(">", 0.0, 9.0)  # 只设 alert


def test_validate_allows_warn_only_lower_direction():
    validate_limit("<", 3.0, 0.0)  # 越低越差，只设 warn


def test_validate_rejects_both_empty():
    with pytest.raises(LimitError):
        validate_limit(">", 0.0, 0.0)


def test_validate_still_checks_order_when_both_set():
    with pytest.raises(LimitError):
        validate_limit(">", 9.0, 9.0)
    with pytest.raises(LimitError):
        validate_limit("<", 1.0, 3.0)


def test_grade_warn_only_never_raises_alert():
    assert grade(5.0, ">", 9.0, 0.0) == "normal"
    assert grade(9.0, ">", 9.0, 0.0) == "warn"
    assert grade(99.0, ">", 9.0, 0.0) == "warn"  # 没有 alert 档，永远不告警


def test_grade_alert_only_has_no_warn_band():
    assert grade(5.0, ">", 0.0, 9.0) == "normal"
    assert grade(9.0, ">", 0.0, 9.0) == "alert"  # 越过就直达告警，中间没有预警档


def test_grade_one_sided_lower_direction():
    assert grade(5.0, "<", 3.0, 0.0) == "normal"
    assert grade(3.0, "<", 3.0, 0.0) == "warn"
    assert grade(0.5, "<", 3.0, 0.0) == "warn"
    assert grade(0.5, "<", 0.0, 3.0) == "alert"


def test_problem_of_ignores_one_sided_rows():
    """单边设置不是「老数据病态」，体检不该报问题。"""
    assert problem_of(">", 9.0, 0.0) is None
    assert problem_of(">", 0.0, 9.0) is None


def test_point_level_treats_both_zero_as_unset():
    """两侧都 0（历史遗留）不算设了阈值，应显示「未设阈值」而不是「正常」。"""
    assert health.point_level(5.0, (0.0, 0.0, ">", "指标")) == "unset"


def test_point_level_one_sided():
    assert health.point_level(99.0, (9.0, 0.0, ">", "指标")) == "warn"
    assert health.point_level(99.0, (0.0, 9.0, ">", "指标")) == "alert"
    assert health.point_level(5.0, (0.0, 9.0, ">", "指标")) == "ok"


def test_thresholds_page_allows_one_sided_input():
    """阈值页不再要求两列同时填。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "thresholds.py").read_text(
        encoding="utf-8"
    )

    assert "warn 与 alert 要同时填" not in src, "旧限制还在"
    assert "float(warn_raw) if warn_raw else 0.0" in src
    assert "float(alert_raw) if alert_raw else 0.0" in src
    assert "留空=该档不判" in src or "留空=不判该档" in src
