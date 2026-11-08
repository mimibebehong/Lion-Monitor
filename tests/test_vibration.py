"""振动 .dat 文件读取接口的行为测试。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.ingest.vibration import (
    VibrationFormatError,
    read_vibration,
    read_vibration_header,
)


def test_reads_header_metadata_from_dat_file(tmp_path: Path) -> None:
    path = write_dat(
        tmp_path / "sample.dat",
        samples=np.zeros(2048, dtype="<f4"),
        channel="CH7",
        sample_rate_hz=25600.0,
        started_at="20260315103000",
    )

    meta = read_vibration_header(path)

    assert meta.channel == "CH7"
    assert meta.sample_rate_hz == pytest.approx(25600.0)
    assert meta.sample_count == 2048
    assert meta.started_at == datetime(2026, 3, 15, 10, 30, 0)
    assert meta.device_ip == "10.10.10.165"
    assert meta.site == "Farm"
    assert meta.rule == "Rule1.d"
    assert meta.path == path


def test_reads_all_samples_as_float32(tmp_path: Path) -> None:
    samples = np.linspace(-1.0, 1.0, 500).astype("<f4")
    path = write_dat(tmp_path / "body.dat", samples=samples)

    record = read_vibration(path)

    assert record.samples.dtype == np.float32
    assert record.samples.shape == (500,)
    assert record.meta.sample_count == 500
    np.testing.assert_array_equal(record.samples, samples)


def test_truncated_body_raises_format_error(tmp_path: Path) -> None:
    samples = np.zeros(100, dtype="<f4")
    path = write_dat(tmp_path / "truncated.dat", samples=samples)
    path.write_bytes(path.read_bytes()[:-8])  # 少 2 个样本

    with pytest.raises(VibrationFormatError, match="长度"):
        read_vibration(path)


def test_header_only_file_readable_by_header_reader(tmp_path: Path) -> None:
    path = write_dat(tmp_path / "header_only.dat", samples=None, sample_count=1000)

    meta = read_vibration_header(path)
    assert meta.sample_count == 1000
    with pytest.raises(VibrationFormatError, match="长度"):
        read_vibration(path)


def test_unsupported_format_version_raises(tmp_path: Path) -> None:
    path = write_dat(tmp_path / "v99.dat", samples=np.zeros(10, dtype="<f4"), version=99)

    with pytest.raises(VibrationFormatError, match="版本"):
        read_vibration_header(path)


def test_duration_and_time_axis_match_sample_rate(tmp_path: Path) -> None:
    samples = np.arange(1000, dtype="<f4")
    path = write_dat(tmp_path / "axis.dat", samples=samples, sample_rate_hz=1000.0)

    record = read_vibration(path)

    assert record.duration_s == pytest.approx(1.0)
    axis = record.time_axis()
    assert axis.shape == (1000,)
    assert axis[0] == pytest.approx(0.0)
    assert axis[-1] == pytest.approx(0.999)


def test_scan_dir_lists_dat_files_sorted_by_start_time(tmp_path: Path) -> None:
    from lion_monitor.ingest.vibration import scan_vibration_dir

    write_dat(tmp_path / "b.dat", samples=np.zeros(10, dtype="<f4"), started_at="20261010120000")
    write_dat(tmp_path / "a.dat", samples=np.zeros(10, dtype="<f4"), started_at="20261010100000")
    (tmp_path / "ignore.txt").write_text("not a vibration file", encoding="utf-8")

    metas = scan_vibration_dir(tmp_path)

    assert [m.started_at.hour for m in metas] == [10, 12]


REAL_DATA_DIR = Path("/mnt/d/hermes_wp/Project/锂电产线/envision/data")


@pytest.mark.skipif(not REAL_DATA_DIR.is_dir(), reason="现场数据目录不可用")
def test_reads_real_envision_recording() -> None:
    files = sorted(REAL_DATA_DIR.glob("*Task1.dat"))
    assert files, "现场数据目录里没有 Task1 文件"

    record = read_vibration(files[0])

    assert record.meta.channel == "CH3"
    assert record.meta.device_ip == "10.10.10.165"
    assert record.meta.sample_rate_hz == pytest.approx(32000.0)
    assert record.meta.sample_count == 320_000
    assert record.duration_s == pytest.approx(10.0)
    assert record.meta.started_at.year == 2026
    assert np.all(np.isfinite(record.samples))
    rms = float(np.sqrt(np.mean(record.samples.astype(np.float64) ** 2)))
    assert 0.5 < rms < 0.7, f"实测 RMS={rms}"


def test_samples_multiplied_by_header_scale(tmp_path: Path) -> None:
    """文件头 scale 是工程单位换算系数：工程值 = 原始值 × scale。"""
    raw = np.array([1.0, -2.0, 3.5, 0.0], dtype="<f4")
    path = write_dat(tmp_path / "scaled.dat", samples=raw, scale=2.5)

    record = read_vibration(path)

    np.testing.assert_allclose(record.samples, raw * 2.5, rtol=0, atol=1e-6)
    assert record.samples.dtype == np.float32
    assert record.meta.scale == pytest.approx(2.5)


def test_scale_one_keeps_samples_unchanged(tmp_path: Path) -> None:
    raw = np.array([0.5, -0.25, 1.75], dtype="<f4")
    path = write_dat(tmp_path / "scale_one.dat", samples=raw, scale=1.0)

    record = read_vibration(path)

    np.testing.assert_array_equal(record.samples, raw)
