"""FFT 幅值谱（方案 9.12）：窗函数归一 / 去均值 / Peak-RMS / 主峰 / 阶次换算 / CSV。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat
from real_data import REAL_CH2_002

from lion_monitor.features.spectrum import (
    AMPLITUDE_TYPES,
    DEFAULT_PEAKS,
    WINDOW_LABELS,
    Spectrum,
    fft_spectrum,
    load_spectrum,
    order_mark_lines,
    spectrum_from_samples,
    top_peaks,
    window_values,
)

FS = 32000.0

pytestmark = pytest.mark.skipif(
    not REAL_CH2_002.is_file(), reason=f"缺少现场实测数据：{REAL_CH2_002}"
)


def _sine(amplitude: float = 0.5, freq: float = 145.0, seconds: float = 20.0) -> np.ndarray:
    n = int(FS * seconds)
    t = np.arange(n) / FS
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_window_labels_cover_the_six_ui_choices():
    assert len(WINDOW_LABELS) == 6
    for must in ("汉宁窗 Hann", "平顶窗 Flattop", "海明窗 Hamming", "布莱克曼窗 Blackman"):
        assert must in WINDOW_LABELS
    assert any("凯泽" in label for label in WINDOW_LABELS)
    assert any("矩形" in label for label in WINDOW_LABELS)


def test_window_values_rejects_unknown_name():
    with pytest.raises(ValueError):
        window_values("自定义窗", 64)


def test_synthetic_145hz_main_peak_within_one_bin_and_2pct():
    """验收 ①：145 Hz / 0.5 g 正弦（32 kHz、20 s）主峰频率 < 1 bin，峰值误差 < 2%。"""
    freqs, amps = fft_spectrum(_sine(), FS)
    bin_hz = float(freqs[1] - freqs[0])
    index = int(np.argmax(amps[1:])) + 1

    assert abs(float(freqs[index]) - 145.0) < bin_hz
    assert abs(float(amps[index]) - 0.5) / 0.5 < 0.02


def test_rms_spectrum_is_peak_over_sqrt2():
    """验收 ②：同一谱线 Peak / RMS 比值 = √2。"""
    _, peak = fft_spectrum(_sine(), FS, amplitude_type="峰值 Peak")
    _, rms = fft_spectrum(_sine(), FS, amplitude_type="有效值 RMS")

    assert AMPLITUDE_TYPES == ("峰值 Peak", "有效值 RMS")
    nonzero = peak > 1e-6
    ratios = peak[nonzero] / rms[nonzero]
    assert np.allclose(ratios, np.sqrt(2.0), rtol=1e-9)


def test_rect_and_hann_peak_within_2pct():
    """验收 ③：加窗后幅值仍准确（相干增益归一），矩形窗与汉宁窗峰值差 < 2%。"""
    _, rect = fft_spectrum(_sine(), FS, window="矩形窗 Rect")
    _, hann = fft_spectrum(_sine(), FS, window="汉宁窗 Hann")

    a_rect = float(rect.max())
    a_hann = float(hann.max())
    assert abs(a_rect - a_hann) / a_hann < 0.02


def test_demean_removes_dc_and_keeps_tone():
    x = _sine(amplitude=0.5) + 2.0

    _, with_dc = fft_spectrum(x, FS, demean=False)
    _, no_dc = fft_spectrum(x, FS, demean=True)

    assert float(with_dc[0]) == pytest.approx(2.0, rel=1e-6)
    assert abs(float(no_dc[0])) < 1e-9
    assert abs(float(no_dc.max()) - 0.5) / 0.5 < 0.02


def test_rejects_unknown_amplitude_type():
    with pytest.raises(ValueError):
        fft_spectrum(_sine(), FS, amplitude_type="峰值-峰值")


def test_top_peaks_sorted_desc_and_spaced():
    freqs, amps = fft_spectrum(_sine(0.5, 100.0) + _sine(0.2, 145.0), FS)

    peaks = top_peaks(freqs, amps, count=3)

    assert peaks[0].freq_hz == pytest.approx(100.0, abs=0.1)
    assert peaks[0].amplitude == pytest.approx(0.5, rel=0.02)
    assert peaks[1].freq_hz == pytest.approx(145.0, abs=0.1)
    assert peaks[1].amplitude == pytest.approx(0.2, rel=0.02)
    assert peaks[0].amplitude > peaks[1].amplitude
    for a, b in zip(peaks, peaks[1:], strict=False):
        assert abs(a.freq_hz - b.freq_hz) > 1.0


def test_order_mark_lines_use_each_target_as_base():
    """验收 ④：目标频率 25 + 阶数 1,2,3 → 竖线 25/50/75 Hz。"""
    marks = order_mark_lines([25.0], [1.0, 2.0, 3.0])

    assert [mark.freq_hz for mark in marks] == pytest.approx([25.0, 50.0, 75.0])
    assert [mark.order for mark in marks] == pytest.approx([1.0, 2.0, 3.0])
    assert {mark.group for mark in marks} == {0}


def test_order_mark_lines_cover_multiple_target_frequencies():
    """多个目标频率：每个都按同一组阶次展开，group 用于同色（马哥 2026-11-03）。"""
    marks = order_mark_lines([25.0, 100.0], [1.0, 2.0])

    assert [(mark.group, mark.freq_hz) for mark in marks] == [
        (0, 25.0),
        (0, 50.0),
        (1, 100.0),
        (1, 200.0),
    ]


def test_order_mark_lines_without_base_frequency_is_empty():
    assert order_mark_lines([], [1.0, 2.0]) == []


def test_spectrum_csv_has_frequency_and_amplitude_columns():
    """验收 ⑤：CSV 导出含 频率, 幅值 两列。"""
    spectrum = spectrum_from_samples(_sine(), FS, label="A · CH2", unit="g")

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


def test_spectrum_peaks_and_resolution():
    spectrum = spectrum_from_samples(_sine(), FS, label="A · CH2", unit="g")

    assert 1 <= len(spectrum.peaks) <= DEFAULT_PEAKS
    assert spectrum.main_peak() is spectrum.peaks[0]
    assert spectrum.peaks[0].freq_hz == pytest.approx(145.0, abs=spectrum.resolution_hz)
    assert spectrum.resolution_hz == pytest.approx(float(np.diff(spectrum.freqs)[0]), rel=1e-9)


def test_load_spectrum_reads_dat_range(tmp_path):
    n = int(FS * 2.0)
    t = np.arange(n) / FS
    samples = (0.5 * np.sin(2 * np.pi * 145.0 * t)).astype("float32")
    path = write_dat(tmp_path / "CH2_Task1.dat", samples=samples, channel="CH2", sample_rate_hz=FS)

    spectrum = load_spectrum(path, 0.0, 2.0, label="A · CH2", unit="g")

    assert spectrum.start_s == pytest.approx(0.0)
    assert spectrum.sample_rate == pytest.approx(FS)
    assert spectrum.peaks[0].freq_hz == pytest.approx(145.0, abs=1.0)
    assert spectrum.peaks[0].amplitude == pytest.approx(0.5, rel=0.02)


@pytest.mark.parametrize("window", WINDOW_LABELS)
def test_every_window_recovers_amplitude(window):
    freqs, amps = fft_spectrum(_sine(), FS, window=window)
    index = int(np.argmax(amps[1:])) + 1

    assert abs(float(freqs[index]) - 145.0) < 1.0
    assert abs(float(amps[index]) - 0.5) / 0.5 < 0.02


def test_real_ds_20261029_002_ch2_main_peak():
    """真实数据实测：DS-20261029-002 CH2 幅值谱主峰（实测 172.05 Hz / ≈1.0 g）。

    卡片原文写「主峰应在 145 Hz 附近（转频 1×）」与本文件实测不符：145 Hz 属包络谱特征
    （见 A3 卡片），幅值谱里 145 Hz 附近幅值仅 1e-5 g。此处按真实数据两条都断言。
    """
    spectrum = load_spectrum(REAL_CH2_002, 0.0, 20.0, label="DS-20261029-002 · CH2", unit="g")

    main = spectrum.peaks[0]
    assert 170.0 < main.freq_hz < 174.0
    assert 0.95 < main.amplitude < 1.05
    near_145 = spectrum.amplitudes[np.abs(spectrum.freqs - 145.0) < 0.5]
    assert float(near_145.max()) < 0.01
