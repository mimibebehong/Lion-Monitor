"""指标常量单一来源：数值不变 + 全仓只在一处定义。

D2（评审 P1-2）：指标码/单位、v_RMS 带通、g→mm/s² 换算系数此前在多处各写一份，
收敛到 ``lion_monitor.constants`` 后由各模块 import 引用。
"""

from __future__ import annotations

import re
from pathlib import Path

from lion_monitor import constants
from lion_monitor.features import metrics as features_metrics
from lion_monitor.store import metric_assign, metric_def

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
APP = ROOT / "app"
DEFINITION_FILE = "src/lion_monitor/constants.py"

#: 指标码 → 单位的字面量表（如 {"v_RMS": "mm/s", ...}）
METRIC_TABLE_LITERAL = re.compile(r'\{\s*"v_RMS"\s*:\s*"mm/s"')
#: 指标码字面量元组（如 ("v_RMS", "a_Peak", "a_RMS")）
METRIC_TUPLE_LITERAL = re.compile(r'\("v_RMS"\s*,\s*"a_Peak"\s*,\s*"a_RMS"\)')


def _py_files() -> list[Path]:
    files: list[Path] = []
    for base in (SRC, APP):
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def _hits(pattern: re.Pattern[str]) -> list[str]:
    """返回 ``相对路径:行号: 内容`` 形式的命中列表。"""
    hits: list[str] = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {line.strip()}")
    return hits


def test_metric_units_values_unchanged() -> None:
    assert constants.METRIC_UNITS == {"v_RMS": "mm/s", "a_Peak": "g", "a_RMS": "g"}
    assert list(constants.METRIC_UNITS) == ["v_RMS", "a_Peak", "a_RMS"]


def test_velocity_constants_values_unchanged() -> None:
    assert constants.VELOCITY_BAND == (10.0, 1000.0)
    assert constants.G_TO_MM_S2 == 9806.65


def test_all_sites_reference_the_same_object() -> None:
    assert metric_def.DEFAULT_METRICS is constants.METRIC_UNITS
    assert metric_assign.METRIC_CODES is constants.METRIC_UNITS
    assert features_metrics.METRIC_UNITS is constants.METRIC_UNITS


def test_metric_table_literal_defined_only_in_constants() -> None:
    files = [hit.split(":")[0] for hit in _hits(METRIC_TABLE_LITERAL)]
    assert files == [DEFINITION_FILE], files


def test_metric_code_tuple_literal_is_gone() -> None:
    assert _hits(METRIC_TUPLE_LITERAL) == []


def test_velocity_constants_defined_only_in_constants() -> None:
    for pattern in (re.compile(r"^\s*G_TO_MM_S2\s*[:=]"), re.compile(r"^\s*VELOCITY_BAND\s*[:=]")):
        files = [hit.split(":")[0] for hit in _hits(pattern)]
        assert files == [DEFINITION_FILE], (pattern.pattern, files)
