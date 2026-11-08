"""FFT 频谱绘图（Plotly）：单边幅值谱、主峰 ●、目标频率/阶次竖线、对比叠线。"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor.features.spectrum import spectrum_from_samples
from lion_monitor.viz.sizing import CHART_HEIGHT
from lion_monitor.viz.spectrum_plot import marker_lanes, spectrum_figure

FS = 32000.0


def _spectrum(amplitude: float = 0.5, freq: float = 145.0, label: str = "A · CH2"):
    n = int(FS * 20.0)
    t = np.arange(n) / FS
    samples = amplitude * np.sin(2 * np.pi * freq * t)
    return spectrum_from_samples(samples, FS, label=label, unit="g")


def test_single_spectrum_figure_layout():
    fig = spectrum_figure([_spectrum()])

    lines = [trace for trace in fig.data if trace.mode == "lines"]
    assert len(lines) == 1
    assert fig.layout.height == CHART_HEIGHT
    assert "幅值" in fig.layout.yaxis.title.text
    assert "频率" in fig.layout.xaxis.title.text
    assert fig.layout.showlegend is False
    assert float(np.max(np.asarray(lines[0].y))) == pytest.approx(0.5, rel=0.02)


def test_main_peak_marked_with_circle():
    fig = spectrum_figure([_spectrum()])

    markers = [trace for trace in fig.data if "markers" in trace.mode]
    assert len(markers) == 1
    assert markers[0].marker.symbol == "circle", "标记点用圆点，不用三角形（马哥 2026-11-03）"
    assert "●" in markers[0].text[0]
    assert "145" in markers[0].text[0]
    assert float(np.asarray(markers[0].x)[0]) == pytest.approx(145.0, abs=0.2)


def test_target_frequency_and_order_lines():
    fig = spectrum_figure(
        [_spectrum()],
        target_freqs=[25.0, 145.0],
        order_marks=[(1.0, 25.0), (2.0, 50.0), (3.0, 75.0)],
    )

    assert len(fig.layout.shapes) == 5
    texts = [annotation.text for annotation in fig.layout.annotations]
    assert any("145" in text for text in texts)
    assert any("2×" in text for text in texts)


def test_two_spectra_overlay_and_legend():
    fig = spectrum_figure([_spectrum(), _spectrum(0.25, label="B · CH2")])

    lines = [trace for trace in fig.data if trace.mode == "lines"]
    assert len(lines) == 2
    assert fig.layout.showlegend is True
    markers = [trace for trace in fig.data if "markers" in trace.mode]
    assert len(markers) == 2


def test_peak_label_position_avoids_axis_overlap():
    """主峰贴左/贴右时标注文字不能居中→溢出绘图区压到纵轴标题与刻度。"""
    left = spectrum_figure([_spectrum(freq=2.0)])
    left_mark = [trace for trace in left.data if "markers" in trace.mode][0]
    assert left_mark.textposition == "top right", "贴左端的主峰文字应右对齐，不能向左溢出"

    right = spectrum_figure([_spectrum(freq=FS / 2 - 5.0)])
    right_mark = [trace for trace in right.data if "markers" in trace.mode][0]
    assert right_mark.textposition == "top left", "贴右端的主峰文字应左对齐，不能向右溢出"

    middle = spectrum_figure([_spectrum(freq=8000.0)])
    mid_mark = [trace for trace in middle.data if "markers" in trace.mode][0]
    assert mid_mark.textposition == "top center"


def test_axes_automargin_keeps_title_clear_of_ticks():
    """纵轴标题靠 automargin 让开刻度，不再与刻度/绘图区重叠。"""
    fig = spectrum_figure([_spectrum()])
    assert fig.layout.yaxis.automargin is True
    assert fig.layout.xaxis.automargin is True


def test_yaxis_leaves_headroom_for_peak_label():
    """主峰幅值接近轴顶时，纵轴要留余量，否则标记文字会被上边缘挤掉。"""
    fig = spectrum_figure([_spectrum(amplitude=0.8)])
    ymax = max(float(np.max(np.asarray(trace.y))) for trace in fig.data if trace.mode == "lines")
    assert fig.layout.yaxis.range[0] == 0
    assert fig.layout.yaxis.range[1] >= ymax * 1.08, "纵轴顶端没留出标注余量"


def test_marker_annotations_staggered_when_clustered():
    """低频段标记线挤在一起时，标注要分层错开，不能全压在同一行。"""
    fig = spectrum_figure(
        [_spectrum()],
        target_freqs=[25.0, 145.0, 290.0],
        order_marks=[(1.0, 25.0), (2.0, 50.0), (3.0, 75.0)],
    )

    annotations = list(fig.layout.annotations)
    assert len(annotations) == 6
    shifts = [a.yshift for a in annotations]
    assert len(set(shifts)) == len(shifts), f"同簇标注高度重复会压字：{shifts}"


def test_marker_annotations_sit_in_bottom_band():
    """标记文字锚在绘图区底部并向上错层：顶部那条带留给主峰标注文字。"""
    fig = spectrum_figure([_spectrum()], target_freqs=[25.0])
    ann = list(fig.layout.annotations)[0]
    assert ann.yanchor == "bottom", "标记文字必须自底边向上排，不能贴顶"
    assert ann.y == 0 and ann.yref == "y domain", "锚点必须显式落在绘图区底边（默认会在顶端）"
    assert ann.yshift > 0


def test_marker_annotations_flat_when_spread():
    """标记线互相离得远时不分层（都在同一高度，图面更整齐）。"""
    fig = spectrum_figure([_spectrum()], target_freqs=[2000.0, 7000.0, 12000.0])

    shifts = [a.yshift for a in fig.layout.annotations]
    assert len(set(shifts)) == 1, f"分散的标注不该错位：{shifts}"


def test_marker_annotations_anchor_away_from_axis():
    """贴左端的标注要向右展（xanchor=left），否则文字会伸进纵轴刻度区。"""
    left = list(spectrum_figure([_spectrum()], target_freqs=[25.0]).layout.annotations)[0]
    assert left.xanchor == "left"
    assert left.yanchor == "bottom"

    right = list(spectrum_figure([_spectrum()], target_freqs=[FS / 2 - 5.0]).layout.annotations)[0]
    assert right.xanchor == "right"

    middle = list(spectrum_figure([_spectrum()], target_freqs=[8000.0]).layout.annotations)[0]
    assert middle.xanchor == "center"


def test_marker_lane_assignment():
    """分层规则：与前一条间距小于横轴 6% 才换层，否则回到第 0 层。"""
    span = 16000.0
    lanes = marker_lanes([25.0, 145.0, 290.0, 9000.0], span)
    assert lanes == [0, 1, 2, 0]


def test_empty_spectrum_list_rejected():
    with pytest.raises(ValueError):
        spectrum_figure([])
