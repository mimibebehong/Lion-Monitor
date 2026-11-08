"""绘图配色与峰值标记（马哥 2026-11-03）：

- 第一条曲线绿色、第二条橙色；
- 主峰数值点用圆点，不用三角形；
- 多个目标频率时，每个目标频率都按同一组阶次画线，同一目标频率的多个阶次用同一种颜色。
"""

from __future__ import annotations

import numpy as np

from lion_monitor.features.spectrum import OrderMark, order_mark_lines, spectrum_from_samples
from lion_monitor.viz.palette import ORDER_COLORS, SERIES_COLORS
from lion_monitor.viz.spectrum_plot import spectrum_figure

FS = 32000.0


def _spectrum(amplitude: float = 0.5, freq: float = 145.0, label: str = "A · CH2"):
    n = int(FS * 2.0)
    t = np.arange(n) / FS
    samples = amplitude * np.sin(2 * np.pi * freq * t)
    return spectrum_from_samples(samples, FS, label=label, unit="g")


def test_first_series_green_second_orange():
    """第一条数据集绿色、第二条橙色。"""
    fig = spectrum_figure([_spectrum(), _spectrum(0.25, label="B · CH2")])
    lines = [trace for trace in fig.data if trace.mode == "lines"]
    assert lines[0].line.color == SERIES_COLORS[0]
    assert lines[1].line.color == SERIES_COLORS[1]
    assert SERIES_COLORS[0].lower() == "#4caf50", "第一条要用绿色"
    assert SERIES_COLORS[1].lower() == "#fb8c00", "第二条要用橙色"


def test_main_peak_marker_is_circle_not_triangle():
    """图上数值标记点用圆点。"""
    fig = spectrum_figure([_spectrum()])
    marker = [trace for trace in fig.data if "markers" in trace.mode][0]
    assert marker.marker.symbol == "circle", "主峰标记必须是圆点，不能用三角形"
    assert "●" in marker.text[0]
    assert "▲" not in marker.text[0]


def test_order_marks_cover_every_target_frequency():
    """两个目标频率时，每个目标频率都要有各自的阶次线。"""
    marks = order_mark_lines([25.0, 100.0], [1.0, 2.0, 3.0])
    assert len(marks) == 6
    by_group: dict[int, list[OrderMark]] = {}
    for mark in marks:
        by_group.setdefault(mark.group, []).append(mark)
    assert [m.freq_hz for m in by_group[0]] == [25.0, 50.0, 75.0]
    assert [m.freq_hz for m in by_group[1]] == [100.0, 200.0, 300.0]


def test_same_target_frequency_orders_share_one_color():
    """同一目标频率的多阶次用同一种颜色，不同目标频率颜色不同。"""
    marks = order_mark_lines([25.0, 100.0], [1.0, 2.0, 3.0])
    fig = spectrum_figure([_spectrum()], order_marks=marks)

    colors: dict[float, str] = {}
    for shape in fig.layout.shapes:
        colors[round(float(shape.x0), 3)] = shape.line.color
    assert colors[25.0] == colors[50.0] == colors[75.0] == ORDER_COLORS[0]
    assert colors[100.0] == colors[200.0] == colors[300.0] == ORDER_COLORS[1]
    assert ORDER_COLORS[0] != ORDER_COLORS[1]


def test_order_marks_labelled_with_order_and_staggered():
    """阶次线要标出阶数（2×），并与目标频率线错层不压字。"""
    marks = order_mark_lines([25.0, 100.0], [1.0, 2.0])
    fig = spectrum_figure([_spectrum()], target_freqs=[25.0, 100.0], order_marks=marks)
    texts = [a.text for a in fig.layout.annotations]
    assert texts.count("1×") == 2 and texts.count("2×") == 2
    shifts = [a.yshift for a in fig.layout.annotations]
    assert len(set(shifts)) == len(shifts), f"标注高度重复会压字：{shifts}"
