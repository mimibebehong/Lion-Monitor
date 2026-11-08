"""复合索引（P3 · S3）：按实际 WHERE / JOIN 模式补 ``metric_value`` 索引，老库迁移不锁表。

取数模式（``store.metrics``，见 P3 / S2 落地）：

- 计数 / 最新值 / 越限行：``WHERE node_id IN (...) AND code IN (...) AND deleted_at IS NULL``
  + ``JOIN channel ON metric_value.channel_id = channel.id``，按 ``(node_id, code)`` 分组、
  组内按 ``computed_at DESC, id DESC`` 取最新 → 索引 ``(node_id, code, computed_at)``
- 逐点取数（``list_values`` / 趋势页）：``channel_id`` + ``code`` + ``computed_at >= 窗口起点``
  → 索引 ``(channel_id, code, computed_at)``

口径：

- 新库：``create_all`` 直接带出这两个索引
- 老库：表已存在时 ``create_all`` 不会补索引，``init_db`` 必须自己幂等补上
  （``e9d79f4``：迁移时游标还占着 ``sqlite_master`` 就执行 DDL 会报 ``database table is locked``，
  所以先取成列表再建）
"""

from __future__ import annotations

import pytest

from lion_monitor.store import db as store_db

#: 目标索引 → 期望的列序（顺序即 SQLite 索引前缀，不能乱）
EXPECTED_INDEXES = {
    "ix_metric_value_node_code_time": ["node_id", "code", "computed_at"],
    "ix_metric_value_channel_code_time": ["channel_id", "code", "computed_at"],
}

#: 老库的单列索引：被复合索引覆盖或没有使用场景，迁移后应当消失（否则计划器会挑它）
OBSOLETE_INDEXES = (
    "ix_metric_value_channel_id",
    "ix_metric_value_code",
    "ix_metric_value_node_id",
    "ix_metric_value_computed_at",
)

LEGACY_TABLE_SQL = (
    "CREATE TABLE metric_value ("
    " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, code VARCHAR(32) NOT NULL,"
    " value FLOAT NOT NULL, unit VARCHAR(16), level VARCHAR(16),"
    " computed_at DATETIME NOT NULL, deleted_at DATETIME,"
    " CONSTRAINT uq_metric_value_point UNIQUE (channel_id, code, computed_at))"
)


def _index_columns(engine) -> dict[str, list[str]]:
    """库里 metric_value 上每个索引 → 列序（按 seqno 排）。"""
    with engine.begin() as conn:
        names = [
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metric_value'"
            ).fetchall()
        ]
        result = {}
        for name in names:
            if name.startswith("sqlite_autoindex"):
                continue
            rows = conn.exec_driver_sql(f"PRAGMA index_info({name})").fetchall()
            result[name] = [row[2] for row in sorted(rows, key=lambda row: row[0])]
    return result


def _count(engine) -> int:
    with engine.begin() as conn:
        return conn.exec_driver_sql("SELECT COUNT(*) FROM metric_value").scalar()


@pytest.fixture()
def legacy_db(tmp_path):
    """老库：只有老唯一约束 + 三个单列索引，且已有数据。"""
    engine = store_db.make_engine(tmp_path / "legacy_idx.db")
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TABLE_SQL)
        for name, column in (
            ("ix_metric_value_channel_id", "channel_id"),
            ("ix_metric_value_code", "code"),
            ("ix_metric_value_computed_at", "computed_at"),
        ):
            conn.exec_driver_sql(f"CREATE INDEX {name} ON metric_value ({column})")
        for point in range(1, 4):
            conn.exec_driver_sql(
                "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
                f" VALUES ({point}, 'v_RMS', {1.0 + point}, 'mm/s', 'unset',"
                f" '2026-10-0{point} 10:00:00')"
            )
    return engine


def test_fresh_db_gets_composite_indexes(tmp_path):
    engine = store_db.make_engine(tmp_path / "fresh_idx.db")
    store_db.init_db(engine)

    indexes = _index_columns(engine)
    for name, columns in EXPECTED_INDEXES.items():
        assert indexes.get(name) == columns
    assert not set(indexes) & set(OBSOLETE_INDEXES)


def test_init_db_is_idempotent_for_indexes(tmp_path):
    """连跑两次不重建、不报错（CREATE INDEX IF NOT EXISTS 语义）。"""
    engine = store_db.make_engine(tmp_path / "twice_idx.db")
    store_db.init_db(engine)
    store_db.init_db(engine)

    indexes = _index_columns(engine)
    for name, columns in EXPECTED_INDEXES.items():
        assert indexes.get(name) == columns


def test_legacy_db_with_indexes_migrates_without_lock(legacy_db):
    """带索引的老库跑迁移：不抛 database table is locked，索引补齐，数据一条不丢。"""
    store_db.init_db(legacy_db)  # 不该抛 OperationalError
    store_db.init_db(legacy_db)  # 幂等

    indexes = _index_columns(legacy_db)
    for name, columns in EXPECTED_INDEXES.items():
        assert indexes.get(name) == columns
    # 老单列索引被复合索引取代，迁移后清掉
    assert not set(indexes) & set(OBSOLETE_INDEXES)
    # 数据原样搬回，历史行 node_id 为空
    assert _count(legacy_db) == 3
    with legacy_db.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT channel_id, value, node_id FROM metric_value ORDER BY channel_id"
        ).fetchall()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert [float(row[1]) for row in rows] == [2.0, 3.0, 4.0]
    assert all(row[2] is None for row in rows)


def test_existing_new_schema_table_gets_indexes_without_rebuild(legacy_db):
    """表结构已是新版、索引还是老的（例如上一轮迁移中途退出）：只改索引，不重建表。"""
    store_db.init_db(legacy_db)
    with legacy_db.begin() as conn:
        for name in EXPECTED_INDEXES:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        for name, column in (
            ("ix_metric_value_channel_id", "channel_id"),
            ("ix_metric_value_code", "code"),
            ("ix_metric_value_node_id", "node_id"),
            ("ix_metric_value_computed_at", "computed_at"),
        ):
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON metric_value ({column})")
        before = conn.exec_driver_sql("SELECT id, value FROM metric_value ORDER BY id").fetchall()

    store_db.init_db(legacy_db)

    indexes = _index_columns(legacy_db)
    for name, columns in EXPECTED_INDEXES.items():
        assert indexes.get(name) == columns
    assert not set(indexes) & set(OBSOLETE_INDEXES)
    with legacy_db.begin() as conn:
        after = conn.exec_driver_sql("SELECT id, value FROM metric_value ORDER BY id").fetchall()
    assert after == before  # 主键与值都没变 → 没整表重建
