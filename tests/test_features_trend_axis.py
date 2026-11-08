"""趋势图横坐标：北京时间 / 测试点数切换（马哥 2026-11-02）。

- 横坐标显示北京时间，刻度只到时分秒；
- 可切到「测试点数」1,2,3…（只为排版），hover 仍给北京时间 + 值，信息量不变。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features import trend_view  # noqa: E402
from lion_monitor.store.metrics import ValueRow  # noqa: E402

STAMPS = (
    "2026-10-29 15:28:35",
    "2026-10-29 15:28:37",
    "2026-10-29 15:28:39",
)


def _rows() -> list[ValueRow]:
    return [
        ValueRow(
            id=index + 1,
            channel_id=1,
            channel_code="CH2",
            dataset_id=1,
            metric_code="v_RMS",
            value=6.0 + index,
            unit="mm/s",
            computed_at=stamp,
        )
        for index, stamp in enumerate(STAMPS)
    ]


def test_beijing_moment_keeps_wall_clock():
    """落库时间本身就是北京时间，不做 ±8 h 换算。"""
    moment = trend_view.beijing_moment("2026-10-29 15:28:35")
    assert moment == datetime(2026, 10, 29, 15, 28, 35)
    assert moment.hour == 15


def test_time_mode_uses_datetimes_and_full_labels():
    x, labels, title = trend_view.axis_points(_rows(), trend_view.X_MODE_TIME)
    assert title == "北京时间"
    assert x == [
        datetime(2026, 10, 29, 15, 28, 35),
        datetime(2026, 10, 29, 15, 28, 37),
        datetime(2026, 10, 29, 15, 28, 39),
    ]
    assert labels[:2] == ["2026-10-29 15:28:35", "2026-10-29 15:28:37"]


def test_index_mode_counts_from_one_but_keeps_time_labels():
    x, labels, title = trend_view.axis_points(_rows(), trend_view.X_MODE_INDEX)
    assert title == "测试点数"
    assert x == [1, 2, 3]
    assert labels == list(STAMPS), "hover 仍要能显示北京时间"


def test_default_mode_is_beijing_time():
    _x, _labels, title = trend_view.axis_points(_rows())
    assert title == "北京时间"
    assert trend_view.X_MODES == ("北京时间", "测试点数")


def test_microsecond_stamps_parse():
    moment = trend_view.beijing_moment("2026-10-29 15:28:35.250000")
    assert moment.microsecond == 250000
