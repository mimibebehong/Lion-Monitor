"""P5-1（S6）：落库 ``metric_value.effective_params_json`` —— 实际生效参数的快照。

``params_hash`` 只能证明「两次计算口径相同」，追不出「当时带通到底是多少」；
这里把夹紧后的生效参数随值落库（老库 ``init_db`` 幂等补列，历史行为空）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat
from sqlalchemy import text

from lion_monitor.features.pipeline import import_batch
from lion_monitor.params import default_params, params_hash
from lion_monitor.store import db as store_db
from lion_monitor.store import metric_assign, tasks, tree

FS = 32000.0
STAMP = "20261031100000"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="生效参数快照")


def _montage(session, task, objects: list[dict]) -> None:
    """装配健康树：每个对象一个通道节点 + 指标（可带覆盖参数）。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    for item in objects:
        comp = tree.create_node(session, task.id, "component", item["对象"], parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", item["通道号"], parent_id=comp.id)
        for code in item["指标"]:
            tree.create_node(
                session, task.id, "metric", code, parent_id=channel.id, attrs={"code": code}
            )
            metric_assign.assign_metric(
                session,
                task.id,
                channel.id,
                code,
                params=item.get("参数", {}).get(code),
            )


def _batch_dir(tmp_path) -> Path:
    """一个批次：100 Hz + 800 Hz 双音（带通上限不同 → v_RMS 不同）。"""
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = (np.sin(2 * np.pi * 100 * t) + 0.8 * np.sin(2 * np.pi * 800 * t)).astype("float32")
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=samples,
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def _raw_rows(session) -> list[tuple[str, str, str, str]]:
    """(code, params_hash, effective_params_json, node_name) 原始行。"""
    return list(
        session.execute(
            text(
                "SELECT code, params_hash, effective_params_json, node_id"
                " FROM metric_value ORDER BY code, node_id"
            )
        ).all()
    )


def test_value_row_carries_clamped_effective_band(session, task, tmp_path):
    """验收 4：band_high=30000 在 fs=32000 下落库快照能读出实际生效到 15200。"""
    _montage(
        session,
        task,
        [
            {
                "对象": "轴承A",
                "通道号": "CH1",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {"band_high": 30000.0}},
            }
        ],
    )
    import_batch(session, task.id, _batch_dir(tmp_path), {})

    rows = _raw_rows(session)
    assert len(rows) == 1
    snapshot = json.loads(rows[0][2])
    assert snapshot["band_high"] == 15200.0
    assert snapshot["band_low"] == 10.0


def test_value_row_hash_includes_code_and_sample_rate(session, task, tmp_path):
    """落库指纹 = params_hash(节点生效参数, code, 本文件采样率)。"""
    _montage(
        session,
        task,
        [{"对象": "轴承A", "通道号": "CH1", "指标": ["v_RMS", "a_RMS"]}],
    )
    import_batch(session, task.id, _batch_dir(tmp_path), {})

    rows = {row[0]: row for row in _raw_rows(session)}
    expected = params_hash(default_params("v_RMS"), code="v_RMS", sample_rate=FS)
    assert rows["v_RMS"][1] == expected
    assert rows["a_RMS"][1] == params_hash(default_params("a_RMS"), code="a_RMS", sample_rate=FS)
    assert rows["v_RMS"][1] != rows["a_RMS"][1]


def test_nodes_with_bands_clamping_to_same_limit_dedupe(session, task, tmp_path):
    """验收 1 的落库体现：16000 与 30000 夹到同一 15200 → 指纹相同、只真算一次。"""
    _montage(
        session,
        task,
        [
            {
                "对象": "轴承A",
                "通道号": "CH1",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {"band_high": 16000.0}},
            },
            {
                "对象": "轴承B",
                "通道号": "CH1",
                "指标": ["v_RMS"],
                "参数": {"v_RMS": {"band_high": 30000.0}},
            },
        ],
    )
    result = import_batch(session, task.id, _batch_dir(tmp_path), {})

    rows = _raw_rows(session)
    assert len(rows) == 2
    assert len({row[1] for row in rows}) == 1  # 指纹相同
    assert {json.loads(row[2])["band_high"] for row in rows} == {15200.0}
    assert result.compute_runs == 1
    assert result.dedupe_hits == 1


def test_metric_value_has_effective_params_column(tmp_path):
    """新库直接建出 effective_params_json 列（文本，默认空串）。"""
    engine = store_db.make_engine(tmp_path / "cols.db")
    store_db.init_db(engine)
    with engine.begin() as conn:
        cols = {row[1]: row for row in conn.exec_driver_sql("PRAGMA table_info(metric_value)")}
    assert "effective_params_json" in cols
    assert cols["effective_params_json"][2] == "TEXT"


def test_legacy_table_gains_blank_effective_params_column(tmp_path):
    """老库（无该列）跑 init_db：补列幂等，历史行留空串。"""
    engine = store_db.make_engine(tmp_path / "legacy.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_value ("
            " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, node_id INTEGER,"
            " code VARCHAR(32) NOT NULL, value FLOAT NOT NULL, unit VARCHAR(16),"
            " level VARCHAR(16), computed_at DATETIME NOT NULL,"
            " params_hash VARCHAR(40) DEFAULT '', rpm_used FLOAT,"
            " rpm_source VARCHAR(16) DEFAULT '', stale INTEGER DEFAULT 0,"
            " deleted_at DATETIME,"
            " CONSTRAINT uq_metric_value_node_point UNIQUE"
            " (channel_id, code, computed_at, node_id))"
        )
        conn.exec_driver_sql(
            "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
            " VALUES (1, 'v_RMS', 2.5, 'mm/s', 'unset', '2026-10-01 10:00:00')"
        )
    store_db.init_db(engine)
    store_db.init_db(engine)  # 幂等

    with engine.begin() as conn:
        cols = {row[1]: row for row in conn.exec_driver_sql("PRAGMA table_info(metric_value)")}
        assert "effective_params_json" in cols
        row = conn.exec_driver_sql(
            "SELECT value, effective_params_json FROM metric_value WHERE code='v_RMS'"
        ).one()
    assert float(row[0]) == pytest.approx(2.5)
    assert row[1] == ""
