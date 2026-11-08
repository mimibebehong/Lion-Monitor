"""T10：SQLite 开 WAL、busy timeout 30 s、写事务一律 ``BEGIN IMMEDIATE``。

现状（docs/reviews/2026-10-31-总体复查.md §6.4）：``journal_mode=delete``，读写互斥，
两个 tab 一个查一个写就有 5 s 级停顿风险；SQLAlchemy 也没设 ``connect_args``。

口径：
- ``make_engine``：``PRAGMA journal_mode=WAL`` + ``PRAGMA synchronous=NORMAL`` +
  ``connect_args={"timeout": 30}``
- 写事务用驱动的 ``isolation_level="IMMEDIATE"``（迁移期也一样），避免 DEFERRED 的
  「读锁升级写锁」互等
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import event

from lion_monitor.store.db import init_db, make_engine

#: 老库表结构（与 data/lion.db 实测一致：唯一约束把 deleted_at 也框进去了）
LEGACY_METRIC_ASSIGN_DDL = """
CREATE TABLE metric_assign (
    id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    metric_code VARCHAR(32) NOT NULL,
    unit VARCHAR(16) NOT NULL,
    params_json TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    deleted_at DATETIME,
    params_locked INTEGER DEFAULT 0,
    PRIMARY KEY (id),
    CONSTRAINT uq_metric_assign UNIQUE (node_id, metric_code, deleted_at)
);
"""


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(tmp_path / "t.db")
    yield eng
    eng.dispose()


def test_engine_uses_wal(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_engine_busy_timeout_is_30_seconds(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000


def test_engine_synchronous_is_normal(engine):
    with engine.connect() as conn:
        # PRAGMA synchronous：0=OFF 1=NORMAL 2=FULL
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1


def test_wal_survives_reconnect(tmp_path):
    path = tmp_path / "t.db"
    first = make_engine(path)
    init_db(first)
    first.dispose()

    second = make_engine(path)
    with second.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    second.dispose()


def test_write_transaction_begins_immediate(engine):
    """驱动发的是 ``BEGIN IMMEDIATE``，不是默认的 ``BEGIN``（DEFERRED）。"""
    traces: list[str] = []
    event.listen(
        engine,
        "connect",
        lambda dbapi_conn, _record: dbapi_conn.set_trace_callback(traces.append),
    )

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO probe (id) VALUES (1)")

    begins = [
        statement.strip().upper()
        for statement in traces
        if statement.strip().upper().startswith("BEGIN")
    ]
    assert begins == ["BEGIN IMMEDIATE"], traces


def test_migration_write_begins_immediate(tmp_path):
    """迁移期的数据搬运（老库重建部分唯一索引）也是 ``BEGIN IMMEDIATE``。"""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(LEGACY_METRIC_ASSIGN_DDL)
    raw.executemany(
        "INSERT INTO metric_assign (id, task_id, node_id, metric_code, unit, params_json,"
        " enabled, deleted_at, params_locked) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, 1, 7, "v_RMS", "mm/s", "{}", 1, None, 0),
            (2, 1, 7, "v_RMS", "mm/s", "{}", 1, None, 0),  # 同键未软删，迁移要处理
        ],
    )
    raw.commit()
    raw.close()

    engine = make_engine(path)
    traces: list[str] = []
    event.listen(
        engine,
        "connect",
        lambda dbapi_conn, _record: dbapi_conn.set_trace_callback(traces.append),
    )
    try:
        init_db(engine)
    finally:
        engine.dispose()

    normalized = " ".join(statement.strip().upper() for statement in traces)
    assert "BEGIN IMMEDIATE" in normalized, traces
    # 迁移真的搬了数据：同键两行只留一条未软删
    with sqlite3.connect(path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM metric_assign WHERE deleted_at IS NULL"
        ).fetchone()[0]
    assert active == 1
