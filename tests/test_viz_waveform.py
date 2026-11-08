"""原始波形绘图（Plotly）：纵轴单位、高度、对比双曲线、抽样生效。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.waveform import UNIT_ACCEL, UNIT_VELOCITY, load_waveform
from lion_monitor.viz.sizing import CHART_HEIGHT
from lion_monitor.viz.waveform_plot import waveform_figure

FS = 32000.0


def _write(tmp_path: Path, label: str, *, amplitude: float = 1.0) -> Path:
    n = int(FS * 2.0)
    t = np.arange(n) / FS
    samples = (amplitude * np.sin(2 * np.pi * 100.0 * t)).astype("float32")
    return write_dat(tmp_path / f"{label}.dat", samples=samples, channel="CH2", sample_rate_hz=FS)


def _series(tmp_path: Path, label: str, *, amplitude: float = 1.0, unit: str = UNIT_ACCEL):
    path = _write(tmp_path, label, amplitude=amplitude)
    return load_waveform(path, 0.0, 2.0, unit=unit, label=label)


def test_single_series_figure(tmp_path):
    series = _series(tmp_path, "A")

    fig = waveform_figure([series], cap=1_000_000)

    assert len(fig.data) == 1
    assert fig.data[0].name == "A"
    assert fig.layout.height == CHART_HEIGHT
    assert fig.layout.yaxis.title.text == UNIT_ACCEL
    assert fig.layout.xaxis.title.text == "时间 (s)"
    assert fig.layout.showlegend is False
    assert np.asarray(fig.data[0].x).size == series.values.size


def test_two_series_figure_shows_legend(tmp_path):
    fig = waveform_figure(
        [_series(tmp_path, "A", amplitude=1.0), _series(tmp_path, "B", amplitude=0.5)],
        cap=1_000_000,
    )

    assert len(fig.data) == 2
    assert fig.layout.showlegend is True


def test_figure_applies_cap(tmp_path):
    series = _series(tmp_path, "A")

    fig = waveform_figure([series], cap=1000)

    assert np.asarray(fig.data[0].x).size <= 1000
    assert fig.data[0].meta["decimated"] is True


def test_figure_uses_velocity_unit(tmp_path):
    velocity = _series(tmp_path, "A", unit=UNIT_VELOCITY)

    fig = waveform_figure([velocity], cap=1_000_000)

    assert fig.layout.yaxis.title.text == UNIT_VELOCITY


def test_figure_rejects_empty_series_list():
    with pytest.raises(ValueError):
        waveform_figure([], cap=1000)
