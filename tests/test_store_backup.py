"""T2 全库备份：在线备份（sqlite3 backup API）+ 保留策略 + 目录口径。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import backup as store_backup  # noqa: E402
from lion_monitor.store import datasets, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


def _seed(db_path: Path, count: int = 3) -> Path:
    """建库 + count 个数据集，返回库文件路径（代表「有业务数据的真库」）。"""
    engine = store_db.make_engine(db_path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="备份任务")
        for idx in range(count):
            datasets.create_dataset(
                session,
                task.id,
                f"批次{idx}",
                f"/data/batch{idx}",
                "2026-11-02 10:00:00",
            )
    engine.dispose()
    return db_path


def _count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]


def test_backup_creates_file_with_same_rows(tmp_path):
    db = _seed(tmp_path / "lion.db", count=3)

    info = store_backup.backup_database(db, tmp_path / "backups", keep=5)

    assert info.path.is_file()
    assert info.path.parent == tmp_path / "backups"
    assert info.path.name.startswith("lion-")
    assert info.path.suffix == ".db"
    assert info.size == info.path.stat().st_size > 0
    assert info.created_at is not None
    # 文件能独立被 sqlite3 打开，行数与原库一致（不是半截 cp）
    assert _count(info.path, "dataset") == _count(db, "dataset") == 3
    with sqlite3.connect(info.path) as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_backup_keeps_newest_n_and_removes_old(tmp_path):
    db = _seed(tmp_path / "lion.db", count=1)
    backups = tmp_path / "backups"

    paths = []
    for _ in range(12):
        existing = {path.name for path in backups.glob("lion-*.db")} if backups.exists() else set()
        info = store_backup.backup_database(db, backups, keep=3)
        # 同一秒内连跑：新备份绝不覆盖还在的备份文件
        assert info.path.name not in existing
        paths.append(info.path)

    left = sorted(backups.glob("lion-*.db"))
    assert len(left) == 3
    assert set(left) == set(paths[-3:])  # 留下的就是最新 3 份


def test_default_dir_and_env_override(tmp_path, monkeypatch):
    db = _seed(tmp_path / "lion.db", count=1)
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)
    assert store_backup.resolve_backup_dir(db) == tmp_path / "backups"

    monkeypatch.setenv("LION_BACKUP_DIR", str(tmp_path / "bk"))
    assert store_backup.resolve_backup_dir(db) == tmp_path / "bk"
    info = store_backup.backup_database(db, keep=2)
    assert info.path.parent == tmp_path / "bk"


def test_list_and_latest_backup(tmp_path, monkeypatch):
    db = _seed(tmp_path / "lion.db", count=1)
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)

    assert store_backup.list_backups(tmp_path / "backups") == []
    assert store_backup.latest_backup(db) is None

    first = store_backup.backup_database(db)
    second = store_backup.backup_database(db)

    rows = store_backup.list_backups(tmp_path / "backups")
    assert [row.path for row in rows] == [second.path, first.path]
    latest = store_backup.latest_backup(db)
    assert latest is not None and latest.path == second.path


def test_missing_db_raises_chinese_error(tmp_path):
    with pytest.raises(store_backup.BackupError) as excinfo:
        store_backup.backup_database(tmp_path / "none.db", tmp_path / "backups")
    assert "库文件不存在" in str(excinfo.value)


def test_unwritable_backup_dir_raises_chinese_error(tmp_path):
    db = _seed(tmp_path / "lion.db", count=1)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir", encoding="utf-8")

    with pytest.raises(store_backup.BackupError) as excinfo:
        store_backup.backup_database(db, blocked)

    assert "备份目录" in str(excinfo.value)


def test_keep_must_be_positive(tmp_path):
    db = _seed(tmp_path / "lion.db", count=1)
    with pytest.raises(store_backup.BackupError):
        store_backup.backup_database(db, tmp_path / "backups", keep=0)
