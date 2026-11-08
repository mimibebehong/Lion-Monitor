"""部分唯一索引（G3）：``metric_assign`` / ``metric_limit`` 同键只允许一行未软删。

SQLite 里 NULL 不等于 NULL，把 ``deleted_at`` 放进唯一列会让约束失效（同键可以插两行，
``metric_jobs`` 字典静默取最后一条）。这里改的是 ``WHERE deleted_at IS NULL`` 的部分唯一索引，
并覆盖老库（带 ``UNIQUE(..., deleted_at)`` 表约束）的迁移。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from lion_monitor.store import db as store_db
from lion_monitor.store import tasks, tree
from lion_monitor.store.models import MetricAssign, MetricLimit

#: 老库表结构（与 data/lion.db 实测一致：唯一约束把 deleted_at 也框进去了）
LEGACY_DDL = (
    """
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
    """,
    """
    CREATE TABLE metric_limit (
        id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        metric_code VARCHAR(32) NOT NULL,
        warn FLOAT NOT NULL,
        alert FLOAT NOT NULL,
        direction VARCHAR(8) NOT NULL,
        enabled INTEGER NOT NULL,
        updated_at DATETIME NOT NULL,
        deleted_at DATETIME,
        PRIMARY KEY (id),
        CONSTRAINT uq_metric_limit UNIQUE (node_id, metric_code, deleted_at)
    );
    """,
)


@pytest.fixture()
def factory(tmp_path):
    engine = store_db.make_engine(tmp_path / "partial.db")
    store_db.init_db(engine)
    return store_db.make_session_factory(engine)


@pytest.fixture()
def node_ids(factory):
    """任务 + 一个通道节点（通道挂在监测对象下面）。"""
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="部分唯一索引")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        return task.id, channel.id


def _assign(task_id: int, node_id: int, code: str = "v_RMS") -> MetricAssign:
    return MetricAssign(
        task_id=task_id,
        node_id=node_id,
        metric_code=code,
        unit="mm/s",
        params_json="{}",
        enabled=1,
    )


def _limit(task_id: int, node_id: int, code: str = "v_RMS") -> MetricLimit:
    return MetricLimit(
        task_id=task_id,
        node_id=node_id,
        metric_code=code,
        warn=4.5,
        alert=11.2,
        direction=">",
        enabled=1,
        updated_at=datetime.now(),
    )


def _index_list(path, table: str) -> dict[str, tuple]:
    with sqlite3.connect(path) as conn:
        return {row[1]: row for row in conn.execute(f"PRAGMA index_list({table})")}


def test_metric_assign_duplicate_active_key_raises(factory, node_ids):
    task_id, node_id = node_ids
    with store_db.session_scope(factory) as session:
        session.add(_assign(task_id, node_id))

    with pytest.raises(IntegrityError):
        with store_db.session_scope(factory) as session:
            session.add(_assign(task_id, node_id))
            session.flush()


def test_metric_assign_reinsert_after_soft_delete(factory, node_ids):
    task_id, node_id = node_ids
    with store_db.session_scope(factory) as session:
        first = _assign(task_id, node_id)
        session.add(first)
        session.flush()
        first.deleted_at = datetime.now()

    with store_db.session_scope(factory) as session:
        session.add(_assign(task_id, node_id))

    with store_db.session_scope(factory) as session:
        rows = session.query(MetricAssign).filter_by(node_id=node_id).all()
        assert len(rows) == 2
        assert [row.deleted_at is None for row in rows].count(True) == 1


def test_metric_limit_duplicate_active_key_raises(factory, node_ids):
    task_id, node_id = node_ids
    with store_db.session_scope(factory) as session:
        session.add(_limit(task_id, node_id))

    with pytest.raises(IntegrityError):
        with store_db.session_scope(factory) as session:
            session.add(_limit(task_id, node_id))
            session.flush()


def test_metric_limit_reinsert_after_soft_delete(factory, node_ids):
    task_id, node_id = node_ids
    with store_db.session_scope(factory) as session:
        first = _limit(task_id, node_id)
        session.add(first)
        session.flush()
        first.deleted_at = datetime.now()

    with store_db.session_scope(factory) as session:
        session.add(_limit(task_id, node_id))

    with store_db.session_scope(factory) as session:
        rows = session.query(MetricLimit).filter_by(node_id=node_id).all()
        assert len(rows) == 2
        assert [row.deleted_at is None for row in rows].count(True) == 1


def test_fresh_db_has_partial_unique_indexes(tmp_path):
    path = tmp_path / "fresh.db"
    store_db.init_db(store_db.make_engine(path))

    for table, index in (
        ("metric_assign", "uq_metric_assign_active"),
        ("metric_limit", "uq_metric_limit_active"),
    ):
        rows = _index_list(path, table)
        assert index in rows, f"{table} 缺部分唯一索引：{sorted(rows)}"
        unique, partial = rows[index][2], rows[index][4]
        assert unique == 1 and partial == 1, rows[index]


def _build_legacy_db(path) -> None:
    with sqlite3.connect(path) as conn:
        for ddl in LEGACY_DDL:
            conn.execute(ddl)
        # metric_assign：3 行同键未软删（复查时人为插过） + 1 行同键已软删
        conn.executemany(
            "INSERT INTO metric_assign (id, task_id, node_id, metric_code, unit,"
            " params_json, enabled, deleted_at, params_locked) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, 1, 7, "v_RMS", "mm/s", "{}", 1, None, 0),
                (2, 1, 7, "v_RMS", "mm/s", "{}", 1, None, 0),
                (3, 1, 7, "v_RMS", "mm/s", "{}", 1, None, 0),
                (4, 1, 7, "v_RMS", "mm/s", "{}", 0, "2026-10-01 09:00:00.000000", 0),
            ],
        )
        # metric_limit：2 行同键未软删 + 1 行同键已软删
        conn.executemany(
            "INSERT INTO metric_limit (id, task_id, node_id, metric_code, warn, alert,"
            " direction, enabled, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (1, 1, 7, "v_RMS", 4.5, 11.2, ">", 1, "2026-10-01 09:00:00.000000", None),
                (2, 1, 7, "v_RMS", 5.0, 12.0, ">", 1, "2026-10-01 09:01:00.000000", None),
                (
                    3,
                    1,
                    7,
                    "v_RMS",
                    1.0,
                    2.0,
                    ">",
                    0,
                    "2026-09-01 09:00:00.000000",
                    "2026-09-02 09:00:00.000000",
                ),
            ],
        )


def _counts(path) -> tuple[int, int]:
    with sqlite3.connect(path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM metric_assign").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM metric_limit").fetchone()[0],
        )


def test_init_db_migrates_legacy_unique_constraint(tmp_path, caplog):
    path = tmp_path / "legacy.db"
    _build_legacy_db(path)
    before = _counts(path)
    assert before == (4, 3)

    store_db.init_db(store_db.make_engine(path))

    # 行数不变（软件删不丢数据）
    assert _counts(path) == before

    with sqlite3.connect(path) as conn:
        kept_assign = conn.execute(
            "SELECT id FROM metric_assign WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        kept_limit = conn.execute(
            "SELECT id FROM metric_limit WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        ddl_assign = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_assign'"
        ).fetchone()[0]
        ddl_limit = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_limit'"
        ).fetchone()[0]
        info_assign = conn.execute("PRAGMA index_info(uq_metric_assign_active)").fetchall()
        info_limit = conn.execute("PRAGMA index_info(uq_metric_limit_active)").fetchall()

    # 同键只留最小 id，其余软删
    assert kept_assign == [(1,)]
    assert kept_limit == [(1,)]
    # 老约束已经从表结构里去掉
    assert "deleted_at)" not in ddl_assign.replace(" ", "")
    assert "deleted_at)" not in ddl_limit.replace(" ", "")
    # 部分唯一索引建成，列序就是键列
    assert [row[2] for row in info_assign] == ["node_id", "metric_code"]
    assert [row[2] for row in info_limit] == ["node_id", "metric_code"]

    # 迁移后约束真的生效：同键未软删再插一次就撞车
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: metric_assign"):
        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO metric_assign (task_id, node_id, metric_code, unit,"
                " params_json, enabled, params_locked) VALUES (1, 7, 'v_RMS', 'mm/s', '{}', 1, 0)"
            )
    # 软删后同键可以再插（历史版本允许重建同一授予）
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metric_assign SET deleted_at='2026-11-01' WHERE id=1")
        conn.execute(
            "INSERT INTO metric_assign (task_id, node_id, metric_code, unit,"
            " params_json, enabled, params_locked) VALUES (1, 7, 'v_RMS', 'mm/s', '{}', 1, 0)"
        )


def test_init_db_second_run_is_idempotent(tmp_path):
    path = tmp_path / "legacy2.db"
    _build_legacy_db(path)
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    first = _counts(path)

    store_db.init_db(engine)

    assert _counts(path) == first
    rows = _index_list(path, "metric_assign")
    assert "uq_metric_assign_active" in rows
