"""S1：指标值参数快照 —— params_hash / rpm_used / rpm_source / stale。

追溯口径（方案文档 2.3 / 5.2）：每个指标值都能回答「由哪套参数、用多少转速算的」。
"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat
from sqlalchemy import text

from lion_monitor.features.metrics import store_metrics
from lion_monitor.features.pipeline import import_batch
from lion_monitor.params import default_params, params_hash
from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store

FS = 32000.0
STAMP = "2026-10-31 09:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


def _channel(session, tmp_path, code: str = "CH1"):
    folder = tmp_path / code
    folder.mkdir(exist_ok=True)
    ds = datasets.create_dataset(session, 1, "批次", folder, "2026-10-31 08:00:00")
    return datasets.add_file(session, ds.id, folder / f"{code}_Task1.dat", code, FS, 64000)


def _columns(engine) -> dict[str, tuple]:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_value)").fetchall()
    return {row[1]: row for row in rows}


def test_metric_value_has_trace_columns(tmp_path):
    engine = store_db.make_engine(tmp_path / "cols.db")
    store_db.init_db(engine)
    cols = _columns(engine)
    assert {"params_hash", "rpm_used", "rpm_source", "stale"} <= set(cols)


def test_legacy_metric_value_table_gets_trace_columns(tmp_path):
    """老库（无这 4 列）跑 init_db 后自动补齐，且可重复执行。"""
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_value ("
            " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, code VARCHAR(32) NOT NULL,"
            " value FLOAT NOT NULL, unit VARCHAR(16), level VARCHAR(16),"
            " computed_at DATETIME NOT NULL, deleted_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
            " VALUES (1, 'v_RMS', 2.5, 'mm/s', 'unset', '2026-10-01 10:00:00')"
        )
    store_db.init_db(engine)
    store_db.init_db(engine)  # 幂等
    cols = _columns(engine)
    assert {"params_hash", "rpm_used", "rpm_source", "stale"} <= set(cols)
    # 补列自带 SQL 默认：老行 stale 不是 NULL 而是 0（未过期）
    assert cols["stale"][4] == "0"
    assert cols["params_hash"][4] == "''"
    with engine.begin() as conn:
        # 批次 B-1：没有参数快照的老 v_RMS 行按「旧窗型」处理，启动迁移会把它标成待重算
        # （列默认仍是 0，标记是数据迁移干的）
        assert conn.exec_driver_sql("SELECT stale FROM metric_value").scalar() == 1


def test_upsert_values_writes_params_hash_and_rpm(session, tmp_path):
    ch = _channel(session, tmp_path)
    hashes = {
        "v_RMS": params_hash(default_params("v_RMS")),
        "a_Peak": params_hash({"detrend": True}),
    }

    metric_store.upsert_values(
        session,
        ch.id,
        {"v_RMS": 3.2, "a_Peak": 1.1},
        {"v_RMS": "mm/s", "a_Peak": "g"},
        STAMP,
        param_hashes=hashes,
        rpm_used=1480.0,
        rpm_source="node",
    )

    rows = {r.metric_code: r for r in metric_store.list_values(session, 1, "CH1")}
    assert rows["v_RMS"].params_hash == hashes["v_RMS"]
    assert rows["a_Peak"].params_hash == hashes["a_Peak"]
    assert rows["v_RMS"].rpm_used == pytest.approx(1480.0)
    assert rows["v_RMS"].rpm_source == "node"
    assert rows["v_RMS"].stale is False


def test_upsert_values_single_hash_applies_to_all_rows(session, tmp_path):
    """只给一个字符串时，本次写入的所有指标都带同一个指纹。"""
    ch = _channel(session, tmp_path)
    metric_store.upsert_values(
        session, ch.id, {"v_RMS": 1.0, "a_RMS": 0.5}, {"v_RMS": "mm/s", "a_RMS": "g"}, STAMP,
        param_hashes="abc123abc123",
    )  # fmt: skip
    rows = metric_store.list_values(session, 1, "CH1")
    assert {r.params_hash for r in rows} == {"abc123abc123"}


def test_upsert_values_without_trace_leaves_blank(session, tmp_path):
    """不传追溯信息时写空串/None/0（老调用方行为兼容）。"""
    ch = _channel(session, tmp_path)
    metric_store.upsert_values(session, ch.id, {"v_RMS": 1.0}, {"v_RMS": "mm/s"}, STAMP)
    row = metric_store.list_values(session, 1, "CH1")[0]
    assert row.params_hash == ""
    assert row.rpm_used is None
    assert row.rpm_source == ""
    assert row.stale is False


def test_store_metrics_passes_trace_through(session, tmp_path):
    ch = _channel(session, tmp_path)
    store_metrics(
        session,
        ch.id,
        {"v_RMS": 3.2, "a_Peak": 1.1, "a_RMS": 0.4},
        STAMP,
        param_hashes={"v_RMS": params_hash(default_params("v_RMS"))},
        rpm_used=1490.0,
        rpm_source="measured",
    )
    row = next(r for r in metric_store.list_values(session, 1, "CH1") if r.metric_code == "v_RMS")
    assert row.params_hash == params_hash(default_params("v_RMS"))
    assert row.rpm_source == "measured"


def test_stale_flag_round_trips(session, tmp_path):
    ch = _channel(session, tmp_path)
    metric_store.upsert_values(session, ch.id, {"v_RMS": 3.2}, {"v_RMS": "mm/s"}, STAMP, stale=True)
    assert metric_store.list_values(session, 1, "CH1")[0].stale is True


def test_trend_series_unchanged_for_callers(session, tmp_path):
    """趋势取数仍返回 (时间, 值)，不因追溯字段变形。"""
    ch = _channel(session, tmp_path)
    metric_store.upsert_values(session, ch.id, {"v_RMS": 9.9}, {"v_RMS": "mm/s"}, STAMP)
    assert metric_store.trend_series(session, 1, "CH1", "v_RMS") == [(STAMP, pytest.approx(9.9))]


def test_db_file_untouched_by_trace_write(session, tmp_path):
    """写追溯字段不影响原有唯一约束：同点重写仍只有一行。"""
    ch = _channel(session, tmp_path)
    for _ in range(2):
        metric_store.upsert_values(
            session, ch.id, {"v_RMS": 9.9}, {"v_RMS": "mm/s"}, STAMP, param_hashes="h1"
        )
    # 写函数只 flush 不 commit（事务由调用方持有，P1 / B2）
    session.commit()
    count = session.execute(text("SELECT COUNT(*) FROM metric_value")).scalar()
    assert count == 1


def test_import_batch_writes_params_hash_for_each_metric(session, tmp_path):
    """导入管线按系统硬默认算指标，每行都带各自的参数指纹（无转速时不写 rpm）。"""
    task = tasks.create_task(session, name="追溯任务")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for code in ("v_RMS", "a_RMS"):
        tree.create_node(
            session, task.id, "metric", code, parent_id=channel.id, attrs={"code": code}
        )

    folder = tmp_path / "批次"
    stamp = "20261031090000"
    t = np.arange(int(FS * 2)) / FS
    write_dat(
        folder / f"{stamp}-CH1_Task1.dat",
        samples=np.sin(2 * np.pi * 100 * t).astype("float32"),
        channel="CH1",
        started_at=stamp,
        sample_rate_hz=FS,
    )

    import_batch(session, task.id, folder, {"CH1": channel.id})

    rows = {r.metric_code: r for r in metric_store.list_values(session, task.id, "CH1")}
    assert rows["v_RMS"].params_hash == params_hash(
        default_params("v_RMS"), code="v_RMS", sample_rate=FS
    )
    assert rows["a_RMS"].params_hash == params_hash(
        default_params("a_RMS"), code="a_RMS", sample_rate=FS
    )
    assert rows["v_RMS"].rpm_used is None
    assert rows["v_RMS"].rpm_source == ""
    assert rows["v_RMS"].stale is False
