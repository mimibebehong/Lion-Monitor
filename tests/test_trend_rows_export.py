"""趋势页 CSV 明细行：必须有「监测对象」列，且值按行的节点归属填。

口径（P2 / S1 卡片第 3 条）：

- 「监测对象」= 该行所属通道节点所在的监测对象名
- 没有节点归属的老数据（``node_id`` 为空）→ 该列留空，不报错
- 其余列（通道 / 指标 / 计算时间 / 值 / 单位 / 判定 / 阈值来源）与改造前一致
"""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import trend_rows  # noqa: E402

from lion_monitor.store.metrics import ValueRow  # noqa: E402

LIMIT = (2.2, 3.8, ">", "指标")


def _row(node_id: int | None, value: float, stamp: str, *, stale: bool = False) -> ValueRow:
    return ValueRow(
        id=1,
        channel_id=5,
        channel_code="CH1",
        dataset_id=1,
        metric_code="v_RMS",
        value=value,
        unit="mm/s",
        computed_at=stamp,
        node_id=node_id,
        stale=stale,
    )


def test_detail_rows_carry_object_name_per_row():
    rows = trend_rows.detail_rows(
        [
            _row(11, 3.0, "2026-11-01 10:00:00"),
            _row(11, 4.0, "2026-11-01 10:01:00"),
        ],
        channel="CH1",
        code="v_RMS",
        limit=LIMIT,
        object_of_channel_node={11: "轴承A"},
    )

    assert [row["监测对象"] for row in rows] == ["轴承A", "轴承A"]
    assert tuple(rows[0]) == trend_rows.COLUMNS
    assert rows[1]["判定"] == "严重"  # 4.0 > alert 3.8
    assert rows[0]["判定"] == "警告"  # 3.0 在 warn 2.2 与 alert 3.8 之间
    assert rows[0]["阈值来源"] == "指标"


def test_legacy_rows_leave_object_name_empty():
    rows = trend_rows.detail_rows(
        [_row(None, 9.9, "2026-10-30 10:00:00")],
        channel="CH1",
        code="v_RMS",
        limit=None,
        object_of_channel_node={11: "轴承A"},
    )

    assert rows[0]["监测对象"] == ""  # 老数据没有节点归属 → 留空，不报错
    assert rows[0]["判定"] == "未设阈值"
    assert rows[0]["阈值来源"] == "未设阈值"


def test_detail_rows_mark_legacy_param_rows_with_flag_column():
    """S4A：CSV 导出多一列「旧参数」，stale 行 = 1、正常行 = 0。"""
    rows = trend_rows.detail_rows(
        [
            _row(11, 3.0, "2026-11-01 10:00:00", stale=True),
            _row(11, 3.1, "2026-11-01 10:02:00"),
        ],
        channel="CH1",
        code="v_RMS",
        limit=LIMIT,
        object_of_channel_node={11: "轴承A"},
    )

    assert "旧参数" in trend_rows.COLUMNS
    assert trend_rows.COLUMNS[-1] == "旧参数"
    assert [row["旧参数"] for row in rows] == [1, 0]
    assert tuple(rows[0]) == trend_rows.COLUMNS
