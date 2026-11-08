"""趋势页时间窗 + 最小/最大保峰抽稀（P3 / G14，方案 §14.1 Q6）。

口径（现场拍板，Q6）：

- 趋势页默认只看**最近 30 天**（``window_start(30)``）；选「全部」时返回 ``None``，不加时间条件；
- 显示上限 2 000 点：不超过就原样画，超过才抽稀；
- 抽稀沿用波形页 ``features.waveform.decimate_indices`` 的**最小/最大包络**思路
  （等长分箱，每箱取最小与最大两点），尖峰不会被跳过；
- 抽稀后的点保持原时间顺序，返回的仍是原始 :class:`ValueRow`，图上的 hover / 判级都用真实点；
- CSV 导出始终是全量原始点，**不经过** :func:`chart_points`。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from ..store.metrics import ValueRow
from .waveform import decimate_indices

__all__ = [
    "DEFAULT_MAX_POINTS",
    "DEFAULT_WINDOW_DAYS",
    "WINDOW_CHOICES",
    "X_MODES",
    "X_MODE_INDEX",
    "X_MODE_TIME",
    "axis_points",
    "beijing_moment",
    "chart_points",
    "window_start",
]

#: 趋势图横坐标口径：北京时间 / 测试点数（2026-11-02）
X_MODE_TIME = "北京时间"
X_MODE_INDEX = "测试点数"
X_MODES = (X_MODE_TIME, X_MODE_INDEX)

#: 落库时间戳的解析格式（采集时钟，本身就是北京时间，不做时区换算）
_STAMP_FMT = "%Y-%m-%d %H:%M:%S"
_STAMP_FMT_US = "%Y-%m-%d %H:%M:%S.%f"


def beijing_moment(stamp: str) -> datetime:
    """落库的 ``computed_at`` → 北京时间 ``datetime``（原样壁钟，不加减小时）。"""
    text = str(stamp or "").strip()
    for fmt in (_STAMP_FMT, _STAMP_FMT_US):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"时间戳格式不对：{stamp!r}")


def axis_points(rows: Sequence[ValueRow], mode: str = X_MODE_TIME) -> tuple[list, list[str], str]:
    """把点位换算成横坐标，返回 ``(x 值, 每点 hover 用的北京时间文本, 轴标题)``。

    - ``北京时间``：x 是壁钟时间（分秒可读），hover 也是同一个时间；
    - ``测试点数``：x 是 1,2,3…（只为排版整齐），**hover 仍然显示北京时间 + 值**，
      所以数据点的信息量不变（2026-11-02）。
    """
    moments = [beijing_moment(row.computed_at) for row in rows]
    labels = [moment.strftime(_STAMP_FMT) for moment in moments]
    if mode == X_MODE_INDEX:
        return list(range(1, len(rows) + 1)), labels, X_MODE_INDEX
    return moments, labels, X_MODE_TIME


#: 默认时间窗（天）。Q6：趋势页默认最近 30 天
DEFAULT_WINDOW_DAYS = 30

#: 图上最多画多少点（Q6）。超过就 min/max 抽稀
DEFAULT_MAX_POINTS = 2_000

#: 页面上可选的时间窗：标签 → 天数（0 = 全部，不加时间条件）
WINDOW_CHOICES: dict[str, int] = {
    f"最近 {DEFAULT_WINDOW_DAYS} 天": DEFAULT_WINDOW_DAYS,
    "最近 90 天": 90,
    "全部": 0,
}


def window_start(days: int | None, *, now: datetime | None = None) -> datetime | None:
    """时间窗起点：``days`` 天前；``0`` / ``None`` = 全部（返回 ``None``，不加时间条件）。"""
    if days is None or days <= 0:
        return None
    moment = now if now is not None else datetime.now()
    return moment - timedelta(days=days)


def chart_points(
    rows: Sequence[ValueRow], cap: int = DEFAULT_MAX_POINTS
) -> tuple[list[ValueRow], bool]:
    """按显示上限决定绘图点：返回 (要画的点, 是否抽稀)。

    ``len(rows) <= cap`` 原样返回（同一个列表副本，元素不变）；超上限时等长分箱各取
    最小/最大两点，点数 ≤ ``cap`` 且全序列最小/最大值一定在图里，顺序按时间升序。
    """
    if cap < 2:
        raise ValueError(f"显示上限点数至少 2，当前 {cap}")
    items = list(rows)
    if len(items) <= cap:
        return items, False

    values = np.asarray([row.value for row in items], dtype="float64")
    indices = [int(index) for index in decimate_indices(values, cap)]
    # 每箱先取 min 后取 max，箱内 max 可能早于 min → 按原索引重排，保持时间升序
    picked = [items[index] for index in sorted(indices)]
    return picked, True
