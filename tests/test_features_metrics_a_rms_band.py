"""a_RMS 加 band_low / band_high：声明、默认值、老库迁移、计算都走带通。

马哥 2026-11-02：a_RMS 指标也要有滤波设置，band_low 和 band_high 设置。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features.metrics import compute_metric_series, compute_metrics  # noqa: E402
from lion_monitor.params import PARAM_SCHEMA, default_params  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_def as metric_def_store  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402
from lion_monitor.store.models import MetricDef  # noqa: E402

FS = 32000.0


def _two_tone(seconds: float = 2.0) -> np.ndarray:
    """50 Hz（1 g）+ 3000 Hz（1 g）各一半能量：带通 10–1000 只留 50 Hz。"""
    t = np.arange(int(FS * seconds)) / FS
    return np.sin(2 * np.pi * 50 * t) + np.sin(2 * np.pi * 3000 * t)


def test_a_rms_schema_declares_band():
    keys = [item["key"] for item in PARAM_SCHEMA["a_RMS"]]
    assert "band_low" in keys and "band_high" in keys
    defaults = default_params("a_RMS")
    assert defaults["band_low"] == 10.0
    assert defaults["band_high"] == 1000.0


def test_a_rms_filters_out_of_band_energy():
    x = _two_tone()
    plain = float(np.sqrt(np.mean((x - x.mean()) ** 2)))
    values = compute_metrics(x, FS)
    # 只有 50 Hz 留在带内：1 g 正弦 → 0.707 g
    assert values["a_RMS"] == pytest.approx(0.70710678, rel=1e-3)
    assert values["a_RMS"] < plain * 0.8


def test_a_rms_respects_custom_band():
    x = _two_tone()
    narrow = compute_metrics(x, FS, params={"band_low": 2500.0, "band_high": 3200.0})
    # 只留 3000 Hz 那一层，同样是 0.707 g
    assert narrow["a_RMS"] == pytest.approx(0.70710678, rel=1e-3)


def test_a_rms_series_uses_band_filter():
    x = _two_tone(seconds=4.0)
    series = compute_metric_series(x, FS, codes=["a_RMS"])["a_RMS"]
    assert series, "应该出点"
    for _offset, value in series:
        assert value == pytest.approx(0.70710678, rel=1e-2)


def test_migration_adds_band_params_to_existing_definitions(tmp_path):
    engine = store_db.make_engine(tmp_path / "m.db")
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="带通迁移")
        metric_def_store.seed_defaults(session, task.id)
    with store_db.session_scope(factory) as session:
        row = session.execute(MetricDef.__table__.select().where(MetricDef.code == "a_RMS")).first()
        old_schema = [
            item
            for item in json.loads(row.params_schema_json)
            if item["key"] not in ("band_low", "band_high")
        ]
        old_defaults = {
            key: value
            for key, value in json.loads(row.params_default_json).items()
            if key not in ("band_low", "band_high")
        }
        model = session.get(MetricDef, row.id)
        model.params_schema_json = json.dumps(old_schema, ensure_ascii=False)
        model.params_default_json = json.dumps(old_defaults, ensure_ascii=False)

    added = store_db.migrate_metric_def_band_params(engine)
    assert added == 2

    with store_db.session_scope(store_db.make_session_factory(engine)) as session:
        row = session.execute(MetricDef.__table__.select().where(MetricDef.code == "a_RMS")).first()
        keys = [item["key"] for item in json.loads(row.params_schema_json)]
        defaults = json.loads(row.params_default_json)
    assert "band_low" in keys and "band_high" in keys
    assert defaults["band_low"] == 10.0 and defaults["band_high"] == 1000.0


def test_migration_is_idempotent(tmp_path):
    engine = store_db.make_engine(tmp_path / "i.db")
    store_db.init_db(engine)
    assert store_db.migrate_metric_def_band_params(engine) == 0
