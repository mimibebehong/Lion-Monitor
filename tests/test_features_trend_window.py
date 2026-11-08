"""趋势页时间窗 + min/max 抽稀（P3 / G14 / §14.1 Q6）。

口径：

- 默认时间窗 = 最近 30 天（``window_start(30)``），选「全部」时返回 ``None``
- 显示上限 2000 点；不超过就原样画，超过才按 min/max 抽稀（与波形页 ``decimate_indices`` 同一算法）
- 抽稀后的点必须保峰：全序列最小值/最大值都在图里，且按时间升序
- CSV 导出始终是全量原始点（不经过 ``chart_points``）
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from lion_monitor.features import trend_view
from lion_monitor.store.metrics import ValueRow


def _rows(count: int, *, start="2026-11-01 00:00:00") -> list[ValueRow]:
    base = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    return [
        ValueRow(
            id=index + 1,
            channel_id=1,
            channel_code="CH1",
            dataset_id=1,
            metric_code="v_RMS",
            value=float(index),
            unit="mm/s",
            computed_at=(base + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        for index in range(count)
    ]


def test_window_start_defaults_to_thirty_days():
    now = datetime(2026, 11, 1, 12, 0, 0)

    assert trend_view.window_start(trend_view.DEFAULT_WINDOW_DAYS, now=now) == datetime(
        2026, 10, 2, 12, 0, 0
    )
    assert trend_view.DEFAULT_WINDOW_DAYS == 30


def test_window_start_returns_none_for_all_time():
    assert trend_view.window_start(0) is None


def test_chart_points_keeps_short_series_untouched():
    rows = _rows(100)

    picked, decimated = trend_view.chart_points(rows)

    assert decimated is False
    assert picked == rows


def test_chart_points_decimates_to_the_cap_and_keeps_peaks():
    # 基线用 ±1 的慢波动，注入的 ±尖峰才是全序列真正的极值（否则 index 递增本身就是最大值）
    rows = [_with_value(row, math.sin(index / 50.0)) for index, row in enumerate(_rows(5000))]
    rows[2000] = _with_value(rows[2000], -99.0)
    rows[3000] = _with_value(rows[3000], 42.0)

    picked, decimated = trend_view.chart_points(rows)

    assert decimated is True
    assert len(picked) <= trend_view.DEFAULT_MAX_POINTS
    assert min(row.value for row in picked) == pytest.approx(-99.0)
    assert max(row.value for row in picked) == pytest.approx(42.0)
    assert [row.computed_at for row in picked] == sorted(row.computed_at for row in picked)


def test_chart_points_cap_is_configurable():
    rows = _rows(500)

    picked, decimated = trend_view.chart_points(rows, cap=50)

    assert decimated is True
    assert len(picked) <= 50


def test_chart_points_rejects_a_cap_below_two():
    with pytest.raises(ValueError):
        trend_view.chart_points(_rows(10), cap=1)


def _with_value(row: ValueRow, value: float) -> ValueRow:
    return ValueRow(
        id=row.id,
        channel_id=row.channel_id,
        channel_code=row.channel_code,
        dataset_id=row.dataset_id,
        metric_code=row.metric_code,
        value=value,
        unit=row.unit,
        computed_at=row.computed_at,
    )
