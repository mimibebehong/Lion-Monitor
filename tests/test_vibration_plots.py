"""振动数据绘图接口的行为测试。"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.figure
import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.ingest import VibrationRecord, read_vibration
from lion_monitor.viz import (
    amplitude_spectrum,
    plot_vibration_overview,
    save_vibration_overview,
    stft_db,
    top_peaks,
)


def make_record(
    tmp_path: Path,
    samples: np.ndarray | None = None,
    *,
    sample_rate_hz: float = 32000.0,
    channel: str = "CH3",
) -> VibrationRecord:
    if samples is None:
        samples = np.sin(2 * np.pi * 1000.0 * np.arange(32000) / sample_rate_hz).astype("<f4")
    path = write_dat(
        tmp_path / f"{channel}.dat",
        samples=samples,
        sample_rate_hz=sample_rate_hz,
        channel=channel,
    )
    return read_vibration(path)


def test_overview_returns_figure_with_four_panels(tmp_path: Path) -> None:
    record = make_record(tmp_path)

    fig = plot_vibration_overview(record)

    assert isinstance(fig, matplotlib.figure.Figure)
    panel_titles = [ax.get_title(loc="left") for ax in fig.axes if ax.get_title(loc="left")]
    assert len(panel_titles) == 4, f"应有 4 个数据面板，实际: {panel_titles}"
    assert all(panel_titles), f"子图标题不应为空: {panel_titles}"
    assert any(ax.get_label() == "<colorbar>" for ax in fig.axes), "时频图应有色标"


def test_amplitude_spectrum_peak_matches_sine_frequency(tmp_path: Path) -> None:
    fs, f0, n = 32000.0, 1000.0, 32768
    sine = np.sin(2 * np.pi * f0 * np.arange(n) / fs).astype("<f4")
    record = make_record(tmp_path, samples=sine, sample_rate_hz=fs)

    freqs, amp = amplitude_spectrum(record.samples, record.meta.sample_rate_hz)

    peak_hz = float(freqs[int(np.argmax(amp))])
    assert peak_hz == pytest.approx(f0, rel=0.01), f"主峰应在 {f0} Hz，实际 {peak_hz}"
    assert float(amp.max()) == pytest.approx(1.0, rel=0.05), "单位幅值正弦的幅值谱峰值应约为 1"


def test_stft_grid_shapes_are_consistent(tmp_path: Path) -> None:
    record = make_record(tmp_path)

    times, freqs, db = stft_db(record.samples, record.meta.sample_rate_hz, nperseg=1024)

    assert db.shape == (freqs.size, times.size)
    assert times[0] > 0
    assert np.all(np.isfinite(db))
    assert float(db.max()) == pytest.approx(0.0, abs=1e-9)


def test_save_writes_png_file(tmp_path: Path) -> None:
    record = make_record(tmp_path)
    target = tmp_path / "figures" / "overview.png"

    saved = save_vibration_overview(record, target)

    assert saved == target
    assert target.exists()
    assert target.stat().st_size > 20_000, "PNG 过小，可能只画了空白画布"
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_top_peaks_finds_dominant_tones_by_amplitude() -> None:
    fs, n = 32000.0, 32768
    t = np.arange(n) / fs
    signal = 1.0 * np.sin(2 * np.pi * 1000.0 * t) + 0.4 * np.sin(2 * np.pi * 2500.0 * t)

    freqs, amp = amplitude_spectrum(signal, fs)
    peaks = top_peaks(freqs, amp, count=2)

    assert len(peaks) == 2
    assert peaks[0][0] == pytest.approx(1000.0, abs=2.0), "第一峰应为幅值最大的 1 kHz"
    assert peaks[0][1] == pytest.approx(1.0, rel=0.05)
    assert peaks[1][0] == pytest.approx(2500.0, abs=2.0)
    assert peaks[1][1] == pytest.approx(0.4, rel=0.1)


def test_waveform_envelope_covers_signal_extremes(tmp_path: Path) -> None:
    fs, n = 32000.0, 32000
    t = np.arange(n) / fs
    signal = 0.8 * np.sin(2 * np.pi * 1500.0 * t) + 0.1 * np.sin(2 * np.pi * 97.0 * t)
    signal = signal.astype("<f4")
    record = make_record(tmp_path, samples=signal, sample_rate_hz=fs)

    fig = plot_vibration_overview(record)

    env_ax = next(ax for ax in fig.axes if ax.get_title(loc="left").startswith("全时波形"))
    ys = np.concatenate([c.get_paths()[0].vertices[:, 1] for c in env_ax.collections])
    assert float(ys.max()) == pytest.approx(float(signal.max()), rel=0.02)
    assert float(ys.min()) == pytest.approx(float(signal.min()), rel=0.02)
