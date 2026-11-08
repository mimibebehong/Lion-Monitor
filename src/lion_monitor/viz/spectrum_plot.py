"""FFT 单边幅值谱（Plotly；现场要求绘图不用 pyplot）。

- 横轴频率、纵轴幅值（单位写进 ``yaxis.title``）；
- 主峰用圆点(●)标注并标出频率/幅值；对比时两组谱叠画，图例贴图顶左端横排；
- 目标频率画竖线并标数值；每个目标频率的 n 阶画在 ``n × 该频率``，标 ``n×``，
  同一目标频率的多个阶次用同一种颜色（2026-11-03）。
"""

from __future__ import annotations

from collections.abc import Sequence

import plotly.graph_objects as go

from ..features.spectrum import OrderMark, Spectrum
from .palette import ORDER_COLORS, SERIES_COLORS
from .sizing import CHART_HEIGHT

__all__ = ["marker_lanes", "marker_xanchor", "peak_text_position", "spectrum_figure"]

#: 标记线配色（目标频率竖线：灰）
_LINE_COLOR = "#8a8f98"


#: 主峰文字避让阈值：峰落在左右 15% 近边缘时改对齐方向
#: （居中会让「▲ 2.0 Hz / 0.82 g」这类长文字溢出绘图区，压住纵轴标题与刻度）
_PEAK_EDGE_RATIO = 0.15


def peak_text_position(freq: float, low: float, high: float) -> str:
    """主峰标注文字的对齐方式：近左端右对齐、近右端左对齐，其余居中。"""
    span = float(high) - float(low)
    if span <= 0:
        return "top center"
    ratio = (float(freq) - float(low)) / span
    if ratio < _PEAK_EDGE_RATIO:
        return "top right"
    if ratio > 1 - _PEAK_EDGE_RATIO:
        return "top left"
    return "top center"


#: 标注分层：相邻标记线间距小于横轴该比例时换一行，避免文字互相压住
_ANNOT_GAP_RATIO = 0.06
#: 标记文字放在绘图区**底部带**（底边往上 6 px 起，每层 +14 px）：
#: - 顶部那条带留给主峰 ▲ 文字；对比模式下有两条主峰文字会向下排到第二行，
#:   标记若也贴顶就会互相盖住（实测过）
#: - 不设层数上限：6 条标记线挤在低频段时，回卷到第 0 层会再次压字（也实测过）
_ANNOT_BOTTOM_SHIFT = 6
_ANNOT_LANE_STEP = 14


def _top_with_headroom(spectrums: Sequence[Spectrum]) -> float:
    """纵轴顶端：最高幅值再留 15% 余量，给 ▲ 主峰标注和顶部标记线文字腾地方。"""
    peak = max(float(spectrum.amplitudes.max()) for spectrum in spectrums)
    return peak * 1.15 if peak > 0 else 1.0


#: 标注水平锚点阈值：落在左边这个比例内就右展（xanchor=left），右边同理
_ANNOT_EDGE_RATIO = 0.08


def marker_xanchor(freq: float, low: float, high: float) -> str:
    """标记文字的横向锚点：近左端向右展、近右端向左展，其余居中。

    居中锚点会让「目标 25 Hz」这种文字伸出绘图区，压住纵轴刻度。
    """
    span = float(high) - float(low)
    if span <= 0:
        return "center"
    ratio = (float(freq) - float(low)) / span
    if ratio < _ANNOT_EDGE_RATIO:
        return "left"
    if ratio > 1 - _ANNOT_EDGE_RATIO:
        return "right"
    return "center"


def marker_lanes(freqs: Sequence[float], span: float) -> list[int]:
    """给按频率排好序的标记线分层：与前一条太近就换层，否则回到第 0 层。

    低频段几条标记线常常只差几十 Hz（如 25 / 145 / 290 Hz 在 0~16000 Hz 轴上），
    文字同高会糊成一片。``span`` 是横轴宽度（Hz）。
    """
    lanes: list[int] = []
    lane = 0
    last: float | None = None
    gap = max(float(span), 0.0) * _ANNOT_GAP_RATIO
    for freq in freqs:
        if last is not None and abs(float(freq) - last) < gap:
            lane += 1
        else:
            lane = 0
        lanes.append(lane)
        last = float(freq)
    return lanes


def _as_order_mark(mark: OrderMark | tuple[float, ...]) -> tuple[float, float, int]:
    """兼容 ``OrderMark`` 与 ``(阶次, Hz)`` 两种写法，统一成 ``(阶次, Hz, 组号)``。"""
    if isinstance(mark, OrderMark):
        return mark.order, mark.freq_hz, mark.group
    order, freq = float(mark[0]), float(mark[1])
    group = int(mark[2]) if len(mark) > 2 else 0
    return order, freq, group


def spectrum_figure(
    spectrums: Sequence[Spectrum],
    *,
    target_freqs: Sequence[float] = (),
    order_marks: Sequence[OrderMark | tuple[float, ...]] = (),
    height: int = CHART_HEIGHT,
) -> go.Figure:
    """把一（或两，对比时）组幅值谱画成 Plotly 图，并叠目标频率/阶次竖线。"""
    if not spectrums:
        raise ValueError("至少需要一条谱线")

    fig = go.Figure()
    unit = spectrums[0].unit
    for index, spectrum in enumerate(spectrums):
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        fig.add_trace(
            go.Scatter(
                x=spectrum.freqs,
                y=spectrum.amplitudes,
                mode="lines",
                name=spectrum.label,
                line={"color": color, "width": 1},
                hovertemplate="%{x:.2f} Hz<br>%{y:.4g} " + unit + "<extra></extra>",
            )
        )
        peak = spectrum.main_peak()
        if peak is not None:
            fig.add_trace(
                go.Scatter(
                    x=[peak.freq_hz],
                    y=[peak.amplitude],
                    mode="markers+text",
                    name=f"{spectrum.label} 主峰",
                    marker={"symbol": "circle", "size": 9, "color": color},
                    text=[f"● {peak.freq_hz:.1f} Hz / {peak.amplitude:.4g} {unit}"],
                    textposition=peak_text_position(
                        peak.freq_hz, float(spectrum.freqs.min()), float(spectrum.freqs.max())
                    ),
                    textfont={"size": 10, "color": color},
                    hoverinfo="skip",
                    showlegend=False,
                )
            )

    top = max(float(spectrum.freqs.max()) for spectrum in spectrums)
    # 目标频率与阶次合并排序后按「层」错开标注高度，低频段挤在一起也不会互相压字
    marks: list[tuple[float, str, str, str]] = []
    for freq in target_freqs:
        marks.append((float(freq), f"目标 {float(freq):g} Hz", "dash", _LINE_COLOR))
    for mark in order_marks:
        order, freq, group = _as_order_mark(mark)
        color = ORDER_COLORS[group % len(ORDER_COLORS)]
        marks.append((freq, f"{order:g}×", "dot", color))
    marks.sort(key=lambda item: item[0])
    lanes = marker_lanes([item[0] for item in marks], top)
    for (freq, text, dash, color), lane in zip(marks, lanes, strict=True):
        fig.add_vline(
            x=freq,
            line={"color": color, "width": 1, "dash": dash},
            annotation_text=text,
            annotation_xanchor=marker_xanchor(freq, 0.0, top),
            # 显式锚到底部：add_vline 默认把标注放在顶端，只给 yanchor 会跑到图外
            annotation_y=0,
            annotation_yref="y domain",
            annotation_yanchor="bottom",
            annotation_yshift=_ANNOT_BOTTOM_SHIFT + _ANNOT_LANE_STEP * lane,
            annotation_font={"size": 9},
        )

    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 12, "t": 30, "b": 10},
        showlegend=len(spectrums) > 1,
        legend={
            "orientation": "h",
            "x": 0,
            "y": 1.02,
            "yanchor": "bottom",
            "font": {"size": 11},
        },
        xaxis={
            "title": {"text": "频率 (Hz)"},
            "automargin": True,
            "range": [0, top],
            "showgrid": True,
            "gridcolor": "#ececec",
        },
        yaxis={
            "title": {"text": f"幅值 ({unit})"},
            "automargin": True,
            "rangemode": "tozero",
            "range": [0, _top_with_headroom(spectrums)],
            "showgrid": True,
            "gridcolor": "#ececec",
        },
        hovermode="x",
    )
    return fig
