"""绘图区高度（2026-11-03：数据分析绘图区再乘 1.5）。

三处必须同一个来源：波形图、频谱图、占位框 —— 少一处就会出现「出图前后高度跳一下」。
"""

from __future__ import annotations

import numpy as np

from lion_monitor.features.spectrum import spectrum_from_samples
from lion_monitor.viz.sizing import BASE_CHART_HEIGHT, CHART_HEIGHT, CHART_HEIGHT_SCALE
from lion_monitor.viz.spectrum_plot import spectrum_figure
from lion_monitor.viz.waveform_plot import waveform_figure

FS = 32000.0


def _spectrum():
    n = int(FS * 1.0)
    t = np.arange(n) / FS
    return spectrum_from_samples(0.5 * np.sin(2 * np.pi * 145.0 * t), FS, label="A", unit="g")


def test_chart_height_is_1_5x_of_base():
    assert BASE_CHART_HEIGHT == 255
    assert CHART_HEIGHT_SCALE == 1.5
    assert CHART_HEIGHT == 383, "255 × 1.5 = 382.5，取整 383"


def test_spectrum_figure_uses_new_height():
    assert spectrum_figure([_spectrum()]).layout.height == CHART_HEIGHT


def test_waveform_figure_uses_new_height():
    from lion_monitor.features.waveform import WaveformSeries

    series = WaveformSeries(
        label="A",
        unit="g",
        sample_rate=FS,
        start_s=0.0,
        times=np.arange(4) / FS,
        values=np.zeros(4),
    )
    assert waveform_figure([series]).layout.height == CHART_HEIGHT


def test_placeholder_uses_same_height_as_figures():
    """占位框与出图后的图必须一样高，否则点运行时会跳一下。"""
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "pages" / "channel_analysis.py"
    ).read_text(encoding="utf-8")
    assert re.search(r"def placeholder\(text: str, height: int = CHART_HEIGHT\)", source), (
        "占位框默认高度要引用 CHART_HEIGHT，不能写死 255"
    )
