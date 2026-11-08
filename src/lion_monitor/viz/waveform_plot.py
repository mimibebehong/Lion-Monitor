"""原始波形折线图（Plotly；现场要求绘图不用 pyplot）。

- 纵轴单位写成 ``yaxis.title``（加速度 a (g) / 速度 v (mm/s)）；
- 超显示上限按最小/最大包络抽样（``features.waveform.sample_points``）；
- 对比时叠两条曲线，图例贴图顶左端横排。
"""

from __future__ import annotations

from collections.abc import Sequence

import plotly.graph_objects as go

from ..features.waveform import DEFAULT_MAX_POINTS, WaveformSeries, sample_points
from .palette import SERIES_COLORS
from .sizing import CHART_HEIGHT

__all__ = ["waveform_figure"]


def waveform_figure(
    series_list: Sequence[WaveformSeries],
    *,
    cap: int = DEFAULT_MAX_POINTS,
    height: int = CHART_HEIGHT,
) -> go.Figure:
    """把一段（或两段，对比时）原始波形画成 Plotly 折线图。"""
    if not series_list:
        raise ValueError("至少需要一条波形曲线")

    fig = go.Figure()
    for index, series in enumerate(series_list):
        times, values, decimated = sample_points(series, cap)
        fig.add_trace(
            go.Scatter(
                x=times,
                y=values,
                mode="lines",
                name=series.label,
                line={"color": SERIES_COLORS[index % len(SERIES_COLORS)], "width": 1},
                meta={"decimated": decimated, "points": series.point_count},
                hovertemplate="%{x:.4f} s<br>%{y:.5g} " + series.unit + "<extra></extra>",
            )
        )

    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 12, "t": 30, "b": 10},
        showlegend=len(series_list) > 1,
        legend={
            "orientation": "h",
            "x": 0,
            "y": 1.02,
            "yanchor": "bottom",
            "font": {"size": 11},
        },
        xaxis={"title": {"text": "时间 (s)"}, "showgrid": True, "gridcolor": "#ececec"},
        yaxis={
            "title": {"text": series_list[0].unit},
            "showgrid": True,
            "gridcolor": "#ececec",
        },
        hovermode="x",
    )
    return fig
