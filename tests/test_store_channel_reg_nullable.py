"""channel_reg.object_id 改可空（自由通道）+ 旧数据回填。

马哥 2026-11-02 口径：传感器有「自由」态，不归属任何监测对象；自由传感器可绑到任意对象。
旧库的 object_id 是 NOT NULL，结构上表达不出自由态，必须重建表；同时把「早已是自由态」的
历史行（无树节点、传感器也没挂节点）回填成 NULL——CH2 就是这种。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import channel_reg, object_registry, sensors, tasks, tree
from lion_monitor.store import db as store_db

OLD_DDL = """
CREATE TABLE channel_reg (
    id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    object_id INTEGER NOT NULL,
    code VARCHAR(32) NOT NULL,
    remark TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(task_id) REFERENCES test_task (id),
    FOREIGN KEY(object_id) REFERENCES monitor_object (id)
)
"""


def _object_id_notnull(engine) -> int:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(channel_reg)").fetchall()
    col = next(row for row in rows if row[1] == "object_id")
    return int(col[3])


def test_fresh_db_allows_null_object_id(tmp_path):
    engine = store_db.make_engine(tmp_path / "new.db")
    store_db.init_db(engine)

    assert _object_id_notnull(engine) == 0


def test_old_db_is_rebuilt_and_rows_kept(tmp_path):
    engine = store_db.make_engine(tmp_path / "old.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE test_task (id INTEGER NOT NULL PRIMARY KEY, code VARCHAR(32),"
            " name VARCHAR(128), remark TEXT, created_at DATETIME, deleted_at DATETIME)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE monitor_object (id INTEGER NOT NULL PRIMARY KEY, task_id INTEGER,"
            " name VARCHAR(128), obj_type VARCHAR(32), part VARCHAR(64), rated_rpm FLOAT,"
            " remark TEXT, created_at DATETIME, deleted_at DATETIME)"
        )
        conn.exec_driver_sql(OLD_DDL)
        conn.exec_driver_sql("INSERT INTO test_task (id, code, name) VALUES (1, 'T1', '任务')")
        conn.exec_driver_sql(
            "INSERT INTO monitor_object (id, task_id, name, obj_type)"
            " VALUES (1, 1, '轴承A', '轴承')"
        )
        conn.exec_driver_sql(
            "INSERT INTO channel_reg (id, task_id, object_id, code, remark, created_at)"
            " VALUES (7, 1, 1, 'CH1', '', '2026-11-01 00:00:00')"
        )

    store_db.init_db(engine)

    assert _object_id_notnull(engine) == 0
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, object_id, code FROM channel_reg").fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [(7, 1, "CH1")]


@pytest.fixture()
def engine(tmp_path):
    instance = store_db.make_engine(tmp_path / "backfill.db")
    store_db.init_db(instance)
    return instance


def test_backfill_frees_channel_whose_sensor_is_free(engine):
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="回填任务")
        obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承")
        channel_reg.create_channel(session, task.id, obj.id, "CH2")
        sensors.create_sensor(
            session, task.id, node_id=None, model="IEPE 500mV/g", channel_code="CH2"
        )
        task_id = task.id

    # 再跑一次迁移：这条通道无树节点、传感器也没挂节点 → 判定为自由态
    store_db.init_db(engine)

    with store_db.session_scope(factory) as session:
        rows = {r.code: r for r in channel_reg.list_channels(session, task_id)}
        assert rows["CH2"].object_id is None
        assert tree.list_nodes(session, task_id)  # 根节点还在，迁移没破坏结构
        node_types = {row.node_type for row in tree.list_nodes(session, task_id)}
        assert "channel" not in node_types
