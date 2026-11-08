"""指标相关常量的唯一来源。

指标码/单位、``v_RMS`` 的速度带通区间、g→mm/s² 换算系数只在这里定义一次，
``store.metric_def`` / ``store.metric_assign`` / ``features.metrics`` / 页面均从这里引用。
"""

from __future__ import annotations

__all__ = ["G_TO_MM_S2", "METRIC_UNITS", "VELOCITY_BAND"]

#: 内置指标码与单位（顺序即界面默认顺序）：v_RMS / a_Peak / a_RMS
METRIC_UNITS: dict[str, str] = {"v_RMS": "mm/s", "a_Peak": "g", "a_RMS": "g"}

#: 速度计算带通（Hz）
VELOCITY_BAND: tuple[float, float] = (10.0, 1000.0)

#: 1 g = 9806.65 mm/s²
G_TO_MM_S2 = 9806.65
