"""指标计算（a_Peak / a_RMS / v_RMS）与指标值落库测试。"""

from __future__ import annotations

import numpy as np
import pytest

from lion_monitor.features.metrics import METRIC_UNITS, compute_metrics, store_metrics
from lion_monitor.store import datasets
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0


def _sine(amplitude: float = 1.0, freq: float = 100.0, seconds: float = 1.0) -> np.ndarray:
    n = int(FS * seconds)
    t = np.arange(n) / FS
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype("float64")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def test_units_declared():
    assert METRIC_UNITS == {"v_RMS": "mm/s", "a_Peak": "g", "a_RMS": "g"}


def test_a_peak_and_rms_of_sine():
    values = compute_metrics(_sine(amplitude=1.0), FS)
    assert values["a_Peak"] == pytest.approx(1.0, rel=1e-3)
    assert values["a_RMS"] == pytest.approx(1.0 / np.sqrt(2), rel=1e-2)


def test_v_rms_grows_with_amplitude():
    low = compute_metrics(_sine(amplitude=0.1, freq=50.0), FS)["v_RMS"]
    high = compute_metrics(_sine(amplitude=0.2, freq=50.0), FS)["v_RMS"]
    assert high == pytest.approx(low * 2, rel=5e-2)


def test_v_rms_of_dc_signal_is_near_zero():
    flat = np.full(int(FS), 0.5)
    assert compute_metrics(flat, FS)["v_RMS"] < 1e-3


def test_v_rms_ignores_high_frequency_outside_band():
    in_band = compute_metrics(_sine(amplitude=1.0, freq=100.0), FS)["v_RMS"]
    out_band = compute_metrics(_sine(amplitude=1.0, freq=5000.0), FS)["v_RMS"]
    assert out_band < in_band * 0.1


def test_compute_metrics_rejects_empty_and_bad_rate():
    with pytest.raises(ValueError):
        compute_metrics(np.array([]), FS)
    with pytest.raises(ValueError):
        compute_metrics(_sine(), 0.0)


def _channel(session, tmp_path, code: str = "CH1"):
    folder = tmp_path / code
    folder.mkdir(exist_ok=True)
    ds = datasets.create_dataset(session, 1, "批次", folder, "2026-10-29 15:28:34")
    return datasets.add_file(session, ds.id, folder / f"{code}_Task1.dat", code, FS, 32000)


def test_store_metrics_writes_rows(session, tmp_path):
    ch = _channel(session, tmp_path)

    store_metrics(
        session,
        ch.id,
        {"v_RMS": 3.2, "a_Peak": 1.1, "a_RMS": 0.4},
        "2026-10-29 15:28:34",
    )

    rows = metric_store.list_values(session, 1, "CH1")
    assert sorted(r.metric_code for r in rows) == ["a_Peak", "a_RMS", "v_RMS"]
    assert next(r for r in rows if r.metric_code == "v_RMS").value == pytest.approx(3.2)
    assert rows[2].dataset_id == ch.dataset_id


def test_store_metrics_is_idempotent_per_point(session, tmp_path):
    ch = _channel(session, tmp_path)

    for _ in range(2):
        store_metrics(
            session, ch.id, {"v_RMS": 3.2, "a_Peak": 1.1, "a_RMS": 0.4}, "2026-10-29 15:28:34"
        )

    assert len(metric_store.list_values(session, 1, "CH1")) == 3


def test_list_values_filtered_by_metric_code(session, tmp_path):
    ch = _channel(session, tmp_path)
    store_metrics(
        session, ch.id, {"v_RMS": 3.2, "a_Peak": 1.1, "a_RMS": 0.4}, "2026-10-29 15:28:34"
    )

    rows = metric_store.list_values(session, 1, "CH1", metric_code="v_RMS")
    assert len(rows) == 1
    assert rows[0].value == pytest.approx(3.2)


def test_trend_series_orders_by_time(session, tmp_path):
    ch = _channel(session, tmp_path)
    for stamp, value in (("2026-10-29 16:10:44", 32.5), ("2026-10-29 15:28:34", 9.9)):
        store_metrics(session, ch.id, {"v_RMS": value}, stamp)

    assert metric_store.trend_series(session, 1, "CH1", "v_RMS") == [
        ("2026-10-29 15:28:34", pytest.approx(9.9)),
        ("2026-10-29 16:10:44", pytest.approx(32.5)),
    ]
