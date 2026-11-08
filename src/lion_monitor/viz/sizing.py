"""绘图区高度（马哥 2026-11-03：数据分析绘图区再乘 1.5）。

波形图、频谱图、占位框共用这一个来源 —— 少一处就会出现「出图前后高度跳一下」。
"""

from __future__ import annotations

__all__ = ["BASE_CHART_HEIGHT", "CHART_HEIGHT", "CHART_HEIGHT_SCALE"]

#: 上一版定稿的绘图区高度
BASE_CHART_HEIGHT = 255
#: 马哥 2026-11-03「绘图区高度再乘以 1.5」
CHART_HEIGHT_SCALE = 1.5
#: 实际高度：255 × 1.5 = 382.5，四舍五入 383（用 int(x+0.5)，round() 会因银行家舍入给 382）
CHART_HEIGHT = int(BASE_CHART_HEIGHT * CHART_HEIGHT_SCALE + 0.5)
