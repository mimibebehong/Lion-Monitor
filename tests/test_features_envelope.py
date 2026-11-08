"""包络谱（方案 9.12）：频域带通（复用 features.filters） → 希尔伯特包络 → 去均值 → FFT + CSV。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat
from real_data import REAL_CH2_001

from lion_monitor.features.envelope import (
    bandpass_filter,
    envelope_spectrum_from_samples,
    hilbert_envelope,
    load_envelope_spectrum,
)
from lion_monitor.features.spectrum import Spectrum

FS = 32000.0

pytestmark = pytest.mark.skipif(
    not REAL_CH2_001.is_file(), reason=f"缺少现场实测数据：{REAL_CH2_001}"
)


def _am(
    modulation: float = 0.5,
    carrier_hz: float = 5000.0,
    mod_hz: float = 25.0,
    seconds: float = 20.0,
) -> np.ndarray:
    """调幅信号：载波 5 kHz、调制 25 Hz，包络 = 1 + m·cos(2π·25t)。"""
    n = int(FS * seconds)
    t = np.arange(n) / FS
    return (1.0 + modulation * np.cos(2 * np.pi * mod_hz * t)) * np.cos(2 * np.pi * carrier_hz * t)


def test_full_band_is_equivalent_to_no_filtering():
    """带通 0 ~ fs/2 = 全频带，等效不做带通。"""
    x = _am()

    y = bandpass_filter(x, FS, 0.0, FS / 2)

    assert np.allclose(y, x)


def test_bandpass_keeps_only_the_band():
    """0.1 g @500 Hz + 0.5 g @5 kHz，带通 4~6 kHz 后只剩 5 kHz 分量（RMS ≈ 0.5/√2）。"""
    n = int(FS * 20.0)
    t = np.arange(n) / FS
    x = 0.1 * np.sin(2 * np.pi * 500.0 * t) + 0.5 * np.sin(2 * np.pi * 5000.0 * t)

    y = bandpass_filter(x, FS, 4000.0, 6000.0)

    assert np.sqrt(np.mean(y**2)) == pytest.approx(0.5 / np.sqrt(2.0), rel=0.05)


def test_bandpass_rejects_inverted_band():
    """验收：上限 ≤ 下限 → 抛错（页面据此告警且不计算，不产生 NaN 图）。"""
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, 10000.0, 2000.0)
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, 2000.0, 2000.0)


def test_bandpass_rejects_out_of_range_edges():
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, -10.0, 2000.0)
    with pytest.raises(ValueError):
        bandpass_filter(_am(), FS, 2000.0, FS)


def test_hilbert_envelope_recovers_modulation_shape():
    """希尔伯特包络 = 调幅信号的调制函数 1 + 0.5·cos，均值 1、谷值 0.5。"""
    env = hilbert_envelope(_am())

    assert env.mean() == pytest.approx(1.0, rel=1e-3)
    assert env.min() == pytest.approx(0.5, rel=0.02)
    assert env.max() == pytest.approx(1.5, rel=0.02)


def test_am_envelope_main_peak_is_25hz():
    """验收：载波 5 kHz / 调制 25 Hz / 32 kHz 采样 → 包络谱主峰 25 Hz（误差 < 1 bin）。"""
    spectrum = envelope_spectrum_from_samples(_am(), FS, label="AM", unit="g")
    bin_hz = float(np.diff(spectrum.freqs)[0])

    main = spectrum.main_peak()
    assert main is not None
    assert main.freq_hz == pytest.approx(25.0, abs=bin_hz)
    assert main.amplitude == pytest.approx(0.5, rel=0.02)


def test_demean_drops_dc_peak_but_keeps_25hz_amplitude():
    """验收：去均值后 0 Hz 直流峰消失，25 Hz 峰幅值不变。"""
    with_dc = envelope_spectrum_from_samples(_am(), FS, demean=False)
    no_dc = envelope_spectrum_from_samples(_am(), FS, demean=True)

    index = int(np.argmin(np.abs(with_dc.freqs - 25.0)))  # 25 Hz 所在分箱（Δf 由 N 决定）
    assert float(with_dc.freqs[index]) == pytest.approx(25.0, abs=0.05)
    assert float(with_dc.amplitudes[0]) == pytest.approx(1.0, rel=1e-3)
    assert abs(float(no_dc.amplitudes[0])) < 1e-6
    assert float(no_dc.amplitudes[index]) == pytest.approx(
        float(with_dc.amplitudes[index]), rel=1e-6
    )
    assert float(no_dc.amplitudes[index]) == pytest.approx(0.5, rel=0.02)


def test_envelope_spectrum_csv_has_frequency_and_amplitude_columns():
    """验收：CSV 导出含 频率, 幅值 两列。"""
    spectrum = envelope_spectrum_from_samples(_am(), FS, label="AM", unit="g")

    assert isinstance(spectrum, Spectrum)
    lines = spectrum.csv_text().strip().splitlines()
    header = lines[0].split(",")

    assert "频率" in header[0]
    assert "幅值" in header[1]
    assert len(lines) == spectrum.freqs.size + 1
    for row in lines[1:3]:
        cells = row.split(",")
        assert len(cells) == 2
        float(cells[0])
        float(cells[1])


def test_load_envelope_spectrum_reads_dat_range(tmp_path):
    n = int(FS * 2.0)
    t = np.arange(n) / FS
    samples = ((1.0 + 0.5 * np.cos(2 * np.pi * 25.0 * t)) * np.cos(2 * np.pi * 5000.0 * t)).astype(
        "float32"
    )
    path = write_dat(tmp_path / "CH2_Task1.dat", samples=samples, channel="CH2", sample_rate_hz=FS)

    spectrum = load_envelope_spectrum(
        path, 0.0, 2.0, label="A · CH2", unit="g", band_low_hz=4000.0, band_high_hz=6000.0
    )

    assert spectrum.start_s == pytest.approx(0.0)
    assert spectrum.sample_rate == pytest.approx(FS)
    main = spectrum.main_peak()
    assert main is not None
    assert main.freq_hz == pytest.approx(25.0, abs=1.0)
    assert main.amplitude == pytest.approx(0.5, rel=0.05)


def test_real_ch2_envelope_lines_145_and_290():
    """真实数据实测：CH2 · 2~10 kHz 带通 → 包络谱出现 145 Hz 与 290 Hz 两条线。

    卡片写的是 `DS-20261029-002` CH2，但实测该条（16:03:13 批次）2~10 kHz 带通的包络谱
    最强线在 344 Hz（= 2×172 Hz 轴系特征），145/290 Hz 处并无显著线；
    145/290 Hz（145.05 / 290.10，为 1×/2×）出现在上一批次 `DS-20261029-001` 的 CH2
    （15:28:34）。此处按真实数据断言，并在结论里说明卡片与实测的差异。
    """
    spectrum = load_envelope_spectrum(
        REAL_CH2_001,
        0.0,
        20.0,
        label="DS-20261029-001 · CH2",
        unit="g",
        band_low_hz=2000.0,
        band_high_hz=10000.0,
    )

    peak_freqs = [peak.freq_hz for peak in spectrum.peaks]
    assert any(abs(freq - 145.0) < 1.0 for freq in peak_freqs), peak_freqs
    assert any(abs(freq - 290.0) < 1.0 for freq in peak_freqs), peak_freqs
