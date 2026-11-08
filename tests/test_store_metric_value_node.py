"""方案 A（2026-10-31 拍板）：指标值按节点存 —— ``metric_value.node_id``。

口径：

- 值按节点落库：``(channel_id, node_id, metric_code)`` 一行；同一通道号挂多个监测对象时各存一份
- 历史行 ``node_id`` 留空（NULL），老库跑 ``init_db`` 自动补列并把唯一约束换成含 node_id 的版本
- ``upsert_values`` 老签名继续可用（写 NULL node_id），行为与改造前一致
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

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
    return datasets.add_file(session, ds.id, folder / f"{code}_Task1.dat", code, 32000.0, 64000)


def _columns(engine) -> dict[str, tuple]:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(metric_value)").fetchall()
    return {row[1]: row for row in rows}


def _create_sql(engine) -> str:
    with engine.begin() as conn:
        return conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_value'"
        ).scalar()


def test_metric_value_has_node_id_column(tmp_path):
    engine = store_db.make_engine(tmp_path / "cols.db")
    store_db.init_db(engine)
    cols = _columns(engine)
    assert "node_id" in cols
    assert cols["node_id"][3] == 0  # notnull=0：历史行留空


def test_unique_constraint_includes_node_id(tmp_path):
    engine = store_db.make_engine(tmp_path / "uq.db")
    store_db.init_db(engine)
    sql = _create_sql(engine)
    assert "uq_metric_value_node_point" in sql


def test_legacy_table_rebuilt_to_allow_two_nodes_per_point(tmp_path):
    """老库（唯一约束只有 channel/code/时间）跑 init_db 后重建，能存两个节点的同一指标点。"""
    engine = store_db.make_engine(tmp_path / "legacy.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_value ("
            " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, code VARCHAR(32) NOT NULL,"
            " value FLOAT NOT NULL, unit VARCHAR(16), level VARCHAR(16),"
            " computed_at DATETIME NOT NULL, deleted_at DATETIME,"
            " CONSTRAINT uq_metric_value_point UNIQUE (channel_id, code, computed_at))"
        )
        conn.exec_driver_sql(
            "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
            " VALUES (1, 'v_RMS', 2.5, 'mm/s', 'unset', '2026-10-01 10:00:00')"
        )
    store_db.init_db(engine)
    store_db.init_db(engine)  # 幂等

    cols = _columns(engine)
    assert set(cols) >= {"params_hash", "rpm_used", "rpm_source", "stale", "node_id"}
    assert cols["stale"][4] == "0"
    assert "uq_metric_value_node_point" in _create_sql(engine)

    with engine.begin() as conn:
        # 老行保住，node_id 为空
        row = conn.exec_driver_sql(
            "SELECT value, node_id FROM metric_value WHERE code='v_RMS'"
        ).one()
        assert float(row[0]) == pytest.approx(2.5)
        assert row[1] is None
        # 同一 (channel, code, 时间) 现在可以存两个节点
        for node_id in (11, 12):
            conn.exec_driver_sql(
                "INSERT INTO metric_value (channel_id, node_id, code, value, unit, level,"
                " computed_at) VALUES (1, ?, 'v_RMS', 3.0, 'mm/s', 'unset', '2026-10-02 10:00:00')",
                (node_id,),
            )
    with engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM metric_value").scalar() == 3


def test_legacy_table_with_indexes_rebuilds(tmp_path):
    """老库通常还带着三个索引（create_all 建的）：重建时先删索引，再建新表（实地库踩过这个坑）。"""
    engine = store_db.make_engine(tmp_path / "idx.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_value ("
            " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, code VARCHAR(32) NOT NULL,"
            " value FLOAT NOT NULL, unit VARCHAR(16), level VARCHAR(16),"
            " computed_at DATETIME NOT NULL, deleted_at DATETIME,"
            " CONSTRAINT uq_metric_value_point UNIQUE (channel_id, code, computed_at))"
        )
        for name, column in (
            ("ix_metric_value_channel_id", "channel_id"),
            ("ix_metric_value_code", "code"),
            ("ix_metric_value_computed_at", "computed_at"),
        ):
            conn.exec_driver_sql(f"CREATE INDEX {name} ON metric_value ({column})")
        conn.exec_driver_sql(
            "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
            " VALUES (7, 'a_Peak', 1.5, 'g', 'unset', '2026-10-03 10:00:00')"
        )

    store_db.init_db(engine)
    store_db.init_db(engine)  # 幂等

    assert "node_id" in _columns(engine)
    assert "uq_metric_value_node_point" in _create_sql(engine)
    with engine.begin() as conn:
        assert (
            conn.exec_driver_sql("SELECT COUNT(*) FROM metric_value WHERE channel_id=7").scalar()
            == 1
        )
        indexes = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metric_value'"
            )
        }
    assert {
        "ix_metric_value_node_code_time",
        "ix_metric_value_channel_code_time",
    } <= indexes


def test_same_node_same_point_still_unique(tmp_path):
    """同节点同时间点不允许重复（重建后唯一约束仍然生效）。"""
    engine = store_db.make_engine(tmp_path / "dup.db")
    store_db.init_db(engine)
    sql = (
        "INSERT INTO metric_value (channel_id, node_id, code, value, unit, level,"
        " computed_at) VALUES (1, 11, 'v_RMS', 3.0, 'mm/s', 'unset', '2026-10-02 10:00:00')"
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)


def test_upsert_node_values_writes_one_row_per_node(session, tmp_path):
    ch = _channel(session, tmp_path)
    written = metric_store.upsert_node_values(
        session,
        ch.id,
        STAMP,
        [
            NodeValue(node_id=11, code="v_RMS", value=3.2, unit="mm/s", params_hash="hh_a"),
            NodeValue(node_id=12, code="v_RMS", value=1.8, unit="mm/s", params_hash="hh_b"),
        ],
    )
    assert written == 2
    rows = metric_store.list_values(session, 1, "CH1")
    assert {r.node_id for r in rows} == {11, 12}
    assert sorted(r.value for r in rows) == [pytest.approx(1.8), pytest.approx(3.2)]


def test_upsert_node_values_replaces_same_point(session, tmp_path):
    """同一通道同一时间点重导：旧行先删后写，不翻倍。"""
    ch = _channel(session, tmp_path)
    for value in (3.2, 9.9):
        metric_store.upsert_node_values(
            session, ch.id, STAMP, [NodeValue(node_id=11, code="v_RMS", value=value, unit="mm/s")]
        )
    rows = metric_store.list_values(session, 1, "CH1")
    assert len(rows) == 1
    assert rows[0].value == pytest.approx(9.9)


def test_upsert_values_legacy_signature_writes_null_node(session, tmp_path):
    """老签名（upsert_values）继续可用，写 node_id 为空的行。"""
    ch = _channel(session, tmp_path)
    metric_store.upsert_values(session, ch.id, {"v_RMS": 1.5}, {"v_RMS": "mm/s"}, STAMP)
    row = metric_store.list_values(session, 1, "CH1")[0]
    assert row.node_id is None


def test_list_values_can_filter_by_node(session, tmp_path):
    ch = _channel(session, tmp_path)
    metric_store.upsert_node_values(
        session,
        ch.id,
        STAMP,
        [
            NodeValue(node_id=11, code="v_RMS", value=3.2, unit="mm/s"),
            NodeValue(node_id=12, code="v_RMS", value=1.8, unit="mm/s"),
        ],
    )
    only = metric_store.list_values(session, 1, "CH1", node_id=12)
    assert [r.value for r in only] == [pytest.approx(1.8)]


def test_mark_stale_marks_only_that_node_and_metric(session, tmp_path):
    ch = _channel(session, tmp_path)
    metric_store.upsert_node_values(
        session,
        ch.id,
        STAMP,
        [
            NodeValue(node_id=11, code="v_RMS", value=3.2, unit="mm/s"),
            NodeValue(node_id=11, code="a_Peak", value=1.1, unit="g"),
            NodeValue(node_id=12, code="v_RMS", value=1.8, unit="mm/s"),
        ],
    )
    assert metric_store.mark_stale(session, 11, "v_RMS") == 1
    stale = {
        (r.node_id, r.metric_code) for r in metric_store.list_values(session, 1, "CH1") if r.stale
    }
    assert stale == {(11, "v_RMS")}


def test_mark_stale_counts_only_fresh_rows(session, tmp_path):
    ch = _channel(session, tmp_path)
    metric_store.upsert_node_values(
        session, ch.id, STAMP, [NodeValue(node_id=11, code="v_RMS", value=3.2, unit="mm/s")]
    )
    assert metric_store.mark_stale(session, 11, "v_RMS") == 1
    assert metric_store.mark_stale(session, 11, "v_RMS") == 0  # 已经 stale 的不重复计数


def test_node_id_points_at_task_node(session, tmp_path):
    """node_id 存的是健康树通道节点 id（不是数据集/通道台账 id）。"""
    task = tasks.create_task(session, name="节点存值")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)

    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-10-31 08:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)

    metric_store.upsert_node_values(
        session,
        file_row.id,
        STAMP,
        [NodeValue(node_id=channel.id, code="v_RMS", value=2.0, unit="mm/s")],
    )
    row = metric_store.list_values(session, task.id, "CH1")[0]
    assert row.node_id == channel.id
    assert session.execute(text("SELECT COUNT(*) FROM task_node")).scalar() == 4
