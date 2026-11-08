"""原始波形上屏：区间读取 / 最小/最大包络抽样 / CSV 导出（方案 9.12）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features import waveform
from lion_monitor.features.metrics import compute_metrics, velocity_series
from lion_monitor.features.waveform import (
    UNIT_ACCEL,
    UNIT_VELOCITY,
    decimate_indices,
    load_waveform,
    sample_points,
)

FS = 32000.0

#: 现场实测样本（CH2，20 s / 640,000 点）
REAL_DIR = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data/四通道保存")
#: DS-20261029-002 的 CH2
REAL_CH2 = REAL_DIR / "2026-10-29 16_03_13" / "2026-10-29 16_03_13-CH2_Task1.dat"
#: DS-20261029-001 的 CH2（「≈0.83 g」实测属这条）
REAL_CH2_001 = REAL_DIR / "2026-10-29 15_28_34" / "2026-10-29 15_28_34-CH2_Task1.dat"


def _write(tmp_path: Path, samples: np.ndarray, channel: str = "CH2") -> Path:
    return write_dat(
        tmp_path / f"{channel}_Task1.dat",
        samples=samples,
        channel=channel,
        sample_rate_hz=FS,
    )


def _sine(amplitude: float = 1.0, freq: float = 100.0, seconds: float = 2.0) -> np.ndarray:
    n = int(FS * seconds)
    t = np.arange(n) / FS
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype("float32")


# ------------------------------------------------------------ 区间读取


def test_load_waveform_reads_only_selected_range(tmp_path):
    x = np.linspace(-1.0, 1.0, 64_000, dtype="float32")
    path = _write(tmp_path, x)

    series = load_waveform(path, 0.5, 1.0, unit=UNIT_ACCEL, label="DS-1 · CH2")

    assert series.label == "DS-1 · CH2"
    assert series.values.size == 16_000
    assert series.times[0] == pytest.approx(0.5)
    assert series.times[-1] == pytest.approx(1.0 - 1.0 / FS)
    np.testing.assert_allclose(series.values, x[16_000:32_000])


def test_load_waveform_clamps_end_to_duration(tmp_path):
    path = _write(tmp_path, _sine(seconds=1.0))

    series = load_waveform(path, 0.0, 5.0, unit=UNIT_ACCEL)

    assert series.values.size == 32_000
    assert series.times[-1] == pytest.approx(1.0 - 1.0 / FS)


def test_load_waveform_rejects_bad_interval(tmp_path):
    path = _write(tmp_path, _sine(seconds=1.0))

    with pytest.raises(ValueError):
        load_waveform(path, 0.5, 0.5, unit=UNIT_ACCEL)
    with pytest.raises(ValueError):
        load_waveform(path, 0.8, 0.2, unit=UNIT_ACCEL)
    with pytest.raises(ValueError):
        load_waveform(path, -0.1, 0.5, unit=UNIT_ACCEL)


def test_load_waveform_rejects_unknown_unit(tmp_path):
    path = _write(tmp_path, _sine(seconds=0.5))

    with pytest.raises(ValueError):
        load_waveform(path, 0.0, 0.5, unit="位移 d (μm)")


# -------------------------------------------------------- 速度口径


def test_velocity_unit_matches_v_rms(tmp_path):
    x = _sine(amplitude=0.2, freq=100.0, seconds=2.0)
    path = _write(tmp_path, x)

    series = load_waveform(path, 0.0, 2.0, unit=UNIT_VELOCITY)

    assert series.unit == UNIT_VELOCITY
    # 100 Hz、0.2 g 正弦 → 速度峰值 = 0.2*9806.65/(2π*100) = 3.12155 mm/s；
    # S4D 后带通是频域置零，通带增益恒为 1，中点峰值与理论值逐位一致（改造前 butter
    # 在 10 Hz 下边沿有暂态，实测 3.30，+6%）
    middle = series.values[series.values.size // 4 : series.values.size * 3 // 4]
    assert float(np.max(np.abs(middle))) == pytest.approx(3.12, rel=1e-3)
    # 与 features/metrics.py 的 v_RMS 同口径
    rms = float(np.sqrt(np.mean(np.square(middle, dtype="float64"))))
    assert rms == pytest.approx(compute_metrics(x, FS)["v_RMS"], rel=0.05)


def test_velocity_unit_of_dc_slice_is_flat(tmp_path):
    path = _write(tmp_path, np.full(64_000, 0.5, dtype="float32"))

    series = load_waveform(path, 0.0, 2.0, unit=UNIT_VELOCITY)

    assert float(np.max(np.abs(series.values))) < 1e-3


def _mixed_signal() -> np.ndarray:
    """20 s：145 Hz 0.9 g（主成分）+ 15 Hz 0.05 g（低端，积分时会被放大）。"""
    n = int(FS * 20)
    t = np.arange(n) / FS
    return (0.9 * np.sin(2 * np.pi * 145.0 * t) + 0.05 * np.sin(2 * np.pi * 15.0 * t)).astype(
        "float32"
    )


def test_velocity_slice_matches_full_record_region(tmp_path):
    """区间速度约等于「整段一次算」的对应区间：积分前左右各多读 0.5 s 边距。"""
    x = _mixed_signal()
    path = _write(tmp_path, x)
    full = velocity_series(x.astype("float64"), FS)

    series = load_waveform(path, 8.0, 10.0, unit=UNIT_VELOCITY)

    assert series.values.size == int(2.0 * FS)
    np.testing.assert_allclose(series.values, full[int(8.0 * FS) : int(10.0 * FS)], atol=1.0)


def test_velocity_padding_is_belt_and_braces_after_frequency_filtering(tmp_path, monkeypatch):
    """S4D 后边距不再是必需：频域带通没有 IIR 暂态，读不读边距结果一致（实测差 7e-14 mm/s）。

    改造前（``butter`` + ``sosfiltfilt``）不读边距会被 10 Hz 低端振铃拉歪 8.3 mm/s，所以旧断言是
    「naive 偏差 > 5 mm/s」。频域置零是循环卷积：本用例的 2 s 区间对 145 / 15 Hz 都是整周期，
    滤波与裁剪可交换，两条路径都回到整段基准。边距机制保留（区间非整周期时仍靠它压接缝）。
    """
    x = _mixed_signal()
    path = _write(tmp_path, x)
    ref = velocity_series(x.astype("float64"), FS)[int(8.0 * FS) : int(10.0 * FS)]

    padded = load_waveform(path, 8.0, 10.0, unit=UNIT_VELOCITY)
    monkeypatch.setattr(waveform, "VELOCITY_EDGE_PAD_S", 0.0)
    naive = load_waveform(path, 8.0, 10.0, unit=UNIT_VELOCITY)

    assert float(np.max(np.abs(padded.values - ref))) < 1.0
    assert float(np.max(np.abs(naive.values - ref))) < 1.0
    assert float(np.max(np.abs(naive.values - padded.values))) < 1e-6


# -------------------------------------------------- 最小/最大包络抽样


def test_decimate_keeps_all_points_when_under_cap():
    x = np.arange(1000, dtype="float64")
    assert decimate_indices(x, 1000).tolist() == list(range(1000))


def test_decimate_keeps_impact_spike():
    n = 1_000_000
    x = np.zeros(n, dtype="float64")
    x[500_001] = 5.0  # 冲击尖峰
    x[123_456] = -4.0  # 负向极值

    picked = decimate_indices(x, 1000)

    assert picked.size <= 1000
    assert x[picked].max() == 5.0
    assert x[picked].min() == -4.0
    assert 500_001 in set(picked.tolist())  # 尖峰那一点本身被选中


def test_decimate_covers_first_and_last_point():
    x = np.arange(100_000, dtype="float64")  # 单调升：首点即最小值、末点即最大值
    picked = decimate_indices(x, 500)

    assert picked.min() == 0
    assert picked.max() == x.size - 1


def test_decimate_rejects_bad_cap():
    with pytest.raises(ValueError):
        decimate_indices(np.arange(100, dtype="float64"), 1)


def test_sample_points_unaffected_under_cap(tmp_path):
    path = _write(tmp_path, _sine(seconds=2.0))
    series = load_waveform(path, 0.0, 2.0, unit=UNIT_ACCEL)

    times, values, decimated = sample_points(series, 1_000_000)

    assert decimated is False
    assert values.size == series.values.size
    np.testing.assert_allclose(times, series.times)


def test_sample_points_decimates_over_cap(tmp_path):
    path = _write(tmp_path, _sine(seconds=2.0))
    series = load_waveform(path, 0.0, 2.0, unit=UNIT_ACCEL)

    times, values, decimated = sample_points(series, 1000)

    assert decimated is True
    assert values.size <= 1000
    assert float(np.max(np.abs(values))) == pytest.approx(
        float(np.max(np.abs(series.values))), rel=1e-6
    )
    assert times[0] >= 0.0 and times[-1] <= 2.0


# ------------------------------------------------------------ CSV 导出


def test_csv_exports_raw_points_not_decimated(tmp_path):
    path = _write(tmp_path, _sine(seconds=2.0))
    series = load_waveform(path, 0.5, 1.0, unit=UNIT_ACCEL)

    # 先做一次很狠的抽稀，导出不受影响
    sample_points(series, 100)
    lines = series.csv_text().strip().splitlines()

    assert lines[0] == f"时间 (s),{UNIT_ACCEL}"
    assert len(lines) == series.values.size + 1
    first = lines[1].split(",")
    assert float(first[0]) == pytest.approx(0.5)
    assert float(first[1]) == pytest.approx(float(series.values[0]))
    assert len(lines[-1].split(",")) == 2


# --------------------------------------------------------- 真实数据实测


@pytest.mark.skipif(not REAL_CH2.exists(), reason="现场样本不在本机")
def test_real_ds002_ch2_20s_peak():
    """DS-20261029-002 的 CH2、20 s 区间：实测峰值 1.11 g（整段 640,000 点全部读到）。"""
    series = load_waveform(REAL_CH2, 0.0, 20.0, unit=UNIT_ACCEL, label="DS-20261029-002 · CH2")

    assert series.values.size == 640_000
    assert float(np.max(np.abs(series.values))) == pytest.approx(1.11, abs=0.02)


@pytest.mark.skipif(not REAL_CH2_001.exists(), reason="现场样本不在本机")
def test_real_ds001_ch2_20s_peak_is_0_83g():
    """卡片里的「≈0.83 g」实测属 DS-20261029-001 的 CH2（0.8294 g），同口径复核。"""
    series = load_waveform(REAL_CH2_001, 0.0, 20.0, unit=UNIT_ACCEL, label="DS-20261029-001 · CH2")

    assert series.values.size == 640_000
    assert float(np.max(np.abs(series.values))) == pytest.approx(0.8294, abs=0.01)
