"""趋势页 CSV 明细行：把逐点值行压成导出表（页面与测试共用）。

口径（P2 / S1）：

- 「监测对象」列按**行归属的通道节点**填（同一通道号挂多个对象时各行各归各的）
- 没有节点归属的老数据（``node_id`` 为空）该列留空，不报错
- 其余列（通道 / 指标 / 采集时间 / 值 / 单位 / 判定 / 阈值来源）与改造前一致
- S4A：「旧参数」列（0/1）标明该值是不是参数 / 转速改过以后没重算的旧值
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import tree_ui

from lion_monitor.features.health import point_level
from lion_monitor.store.metrics import ValueRow

__all__ = ["COLUMNS", "detail_rows"]

#: 导出表格的列顺序（「监测对象」在最前，方便按对象筛选）
COLUMNS: tuple[str, ...] = (
    "监测对象",
    "通道",
    "指标",
    "采集时间",
    "值",
    "单位",
    "判定",
    "阈值来源",
    "旧参数",
)


def detail_rows(
    rows: Sequence[ValueRow],
    *,
    channel: str,
    code: str,
    limit: tuple[float, float, str, str] | None,
    object_of_channel_node: Mapping[int, str],
) -> list[dict]:
    """逐点明细行：一个 ``metric_value`` 行一条，带对象名与判级。"""
    out: list[dict] = []
    for row in rows:
        level = point_level(row.value, limit)
        out.append(
            {
                "监测对象": object_of_channel_node.get(row.node_id, "") if row.node_id else "",
                "通道": channel,
                "指标": code,
                "采集时间": row.computed_at,
                "值": round(row.value, 4),
                "单位": row.unit,
                "判定": tree_ui.STATE_TEXT[level],
                "阈值来源": limit[3] if limit else "未设阈值",
                "旧参数": 1 if row.stale else 0,
            }
        )
    return out
