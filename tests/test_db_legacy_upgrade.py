"""复查 v2 阻塞项 1 回归：老库升级（``sensor.node_id NOT NULL`` + 没有 ``channel_code``）。

缺陷背景（``docs/reviews/2026-11-03-总体复查-v2.md`` 问题 1）：

- ``_relax_sensor_node_nullable`` 排在 ``_add_sensor_channel_code`` 之前，重建表用的是
  **当前 ORM**：新表 ``channel_code`` 是 NOT NULL，老表没这列 → 搬数据时
  ``NOT NULL constraint failed: sensor.channel_code``，老库升级必失败；
- 失败后留下 ``sensor_old`` + 空的 ``sensor``，恢复分支同样不改列，因此每次启动都重试、
  每次都失败（应用打不开）。

本组用例锁三件事：升级成功且行数不丢、连续 3 次 ``init_db()`` 幂等、中途失败的中间态能自愈。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lion_monitor.store import db as store_db
from lion_monitor.store.models import Base

#: 老库的 sensor：``node_id`` 是 NOT NULL，且**没有** ``channel_code`` 列
LEGACY_SENSOR_DDL = """
CREATE TABLE sensor (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    model VARCHAR(128) NOT NULL,
    sensitivity VARCHAR(64) NOT NULL,
    range_g VARCHAR(64) NOT NULL,
    mounting VARCHAR(64) NOT NULL,
    calibrated_on VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    remark TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    deleted_at DATETIME
)
"""

LEGACY_SENSOR_ROWS = 3


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        counts = {}
        for table in ("sensor", "sensor_old", "test_task"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = -1  # 表不存在
        return counts
    finally:
        conn.close()


def _schema(path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()


def _sensor_columns(path: Path) -> dict[str, tuple[str, int]]:
    """列名 → (类型, notnull)；``notnull`` 为 1 表示 NOT NULL。"""
    conn = sqlite3.connect(path)
    try:
        return {
            row[1]: (row[2] or "", int(row[3])) for row in conn.execute("PRAGMA table_info(sensor)")
        }
    finally:
        conn.close()


def build_legacy_db(path: Path, rows: int = LEGACY_SENSOR_ROWS) -> Path:
    """造一个老 schema 库：当前库建好后再把 sensor 换成老结构，塞若干行。"""
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    engine.dispose()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE sensor")
        conn.executescript(LEGACY_SENSOR_DDL)
        for index in range(1, rows + 1):
            conn.execute(
                "INSERT INTO sensor (id, task_id, node_id, model, sensitivity, range_g,"
                " mounting, calibrated_on, status, remark, created_at)"
                " VALUES (?, 1, ?, ?, '100mV/g', '5g', '磁吸', '2026-01-01', '启用', '',"
                " '2026-10-01 08:00:00')",
                (index, 100 + index, f"PCB-{index}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def legacy_db(tmp_path) -> Path:
    return build_legacy_db(tmp_path / "legacy.db")


def test_legacy_upgrade_keeps_rows_and_relaxes_node_id(legacy_db: Path) -> None:
    """升级成功：行数不变、node_id 可空、channel_code 补成空串。"""
    before = _table_counts(legacy_db)
    engine = store_db.make_engine(legacy_db)
    try:
        store_db.init_db(engine)
    finally:
        engine.dispose()

    after = _table_counts(legacy_db)
    assert after["sensor"] == before["sensor"] == LEGACY_SENSOR_ROWS
    assert after["sensor_old"] == -1  # 临时表已清掉

    columns = _sensor_columns(legacy_db)
    assert columns["node_id"][1] == 0  # 已改成可空
    assert columns["channel_code"][1] == 1  # 仍是 NOT NULL
    conn = sqlite3.connect(legacy_db)
    try:
        values = [row[0] for row in conn.execute("SELECT channel_code FROM sensor ORDER BY id")]
        models = [row[0] for row in conn.execute("SELECT model FROM sensor ORDER BY id")]
    finally:
        conn.close()
    assert values == [""] * LEGACY_SENSOR_ROWS
    assert models == [f"PCB-{index}" for index in range(1, LEGACY_SENSOR_ROWS + 1)]


def test_legacy_upgrade_is_idempotent_three_times(legacy_db: Path) -> None:
    """连续 3 次 ``init_db()``：不报错、schema 与行数都不再变化。"""
    engine = store_db.make_engine(legacy_db)
    try:
        store_db.init_db(engine)
        schema_once = _schema(legacy_db)
        counts_once = _table_counts(legacy_db)
        store_db.init_db(engine)
        store_db.init_db(engine)
    finally:
        engine.dispose()

    assert _schema(legacy_db) == schema_once
    assert _table_counts(legacy_db) == counts_once
    assert counts_once["sensor"] == LEGACY_SENSOR_ROWS


def test_half_failed_upgrade_self_heals(tmp_path) -> None:
    """上次升级中途失败（旧表已改名 sensor_old、新表已建但数据没搬）→ 本次自愈。"""
    path = build_legacy_db(tmp_path / "half.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE sensor RENAME TO sensor_old")
        conn.commit()
    finally:
        conn.close()

    # 新表按当前 ORM 建（channel_code NOT NULL 且 DEFAULT ''），0 行
    engine = store_db.make_engine(path)
    with engine.begin() as connection:
        Base.metadata.tables["sensor"].create(connection)
    engine.dispose()
    assert _table_counts(path)["sensor"] == 0
    assert _table_counts(path)["sensor_old"] == LEGACY_SENSOR_ROWS

    engine = store_db.make_engine(path)
    try:
        store_db.init_db(engine)
        store_db.init_db(engine)
    finally:
        engine.dispose()

    counts = _table_counts(path)
    assert counts["sensor"] == LEGACY_SENSOR_ROWS
    assert counts["sensor_old"] == -1


def test_interrupted_before_table_recreate_restores_data(tmp_path) -> None:
    """更早的中断态（只改名、新表还没建）也不许把 sensor_old 直接丢掉。"""
    path = build_legacy_db(tmp_path / "renamed.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE sensor RENAME TO sensor_old")
        conn.commit()
    finally:
        conn.close()

    engine = store_db.make_engine(path)
    try:
        store_db.init_db(engine)
    finally:
        engine.dispose()

    counts = _table_counts(path)
    assert counts["sensor"] == LEGACY_SENSOR_ROWS
    assert counts["sensor_old"] == -1
    assert _sensor_columns(path)["node_id"][1] == 0
