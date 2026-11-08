"""T2 迁移前自动备份：init_db 检测到结构变更时先备份，备份失败就中止迁移。"""

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
from lion_monitor.store import db as store_db  # noqa: E402


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def _legacy_db(path: Path) -> Path:
    """建到最新结构后删掉 sensor.channel_code：下次 init_db 必然要加这一列。"""
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sensor DROP COLUMN channel_code")
    assert "channel_code" not in _columns(path, "sensor")
    return path


def _backups(folder: Path) -> list[Path]:
    return sorted(folder.glob("lion-*.db")) if folder.exists() else []


def test_pending_migrations_detects_without_touching_db(tmp_path, monkeypatch):
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)
    db = _legacy_db(tmp_path / "lion.db")
    engine = store_db.make_engine(db)

    pending = store_db.pending_migrations(engine)

    assert any("sensor" in item for item in pending), pending
    # 探测不能动真库：结构没变、也没偷偷备份
    assert "channel_code" not in _columns(db, "sensor")
    assert _backups(tmp_path / "backups") == []
    engine.dispose()


def test_pending_migrations_empty_when_schema_is_current(tmp_path):
    db = tmp_path / "lion.db"
    engine = store_db.make_engine(db)
    store_db.init_db(engine)

    assert store_db.pending_migrations(engine) == []
    engine.dispose()


def test_init_db_backs_up_before_migration(tmp_path, monkeypatch):
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)
    db = _legacy_db(tmp_path / "lion.db")

    store_db.init_db(store_db.make_engine(db))

    backups = _backups(tmp_path / "backups")
    assert len(backups) == 1
    # 备份里是「迁移前」的结构，真库里是新结构
    assert "channel_code" not in _columns(backups[0], "sensor")
    assert "channel_code" in _columns(db, "sensor")


def test_fresh_db_does_not_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)

    store_db.init_db(store_db.make_engine(tmp_path / "new.db"))

    assert _backups(tmp_path / "backups") == []


def test_second_init_db_does_not_backup_again(tmp_path, monkeypatch):
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)
    db = _legacy_db(tmp_path / "lion.db")
    store_db.init_db(store_db.make_engine(db))
    assert len(_backups(tmp_path / "backups")) == 1

    store_db.init_db(store_db.make_engine(db))

    assert len(_backups(tmp_path / "backups")) == 1


def test_backup_failure_aborts_migration(tmp_path, monkeypatch):
    db = _legacy_db(tmp_path / "lion.db")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("LION_BACKUP_DIR", str(blocked))

    with pytest.raises(store_backup.BackupError) as excinfo:
        store_db.init_db(store_db.make_engine(db))

    assert "迁移中止" in str(excinfo.value)
    assert "备份" in str(excinfo.value)
    # 库结构原封未动：没备份就绝不允许改表
    assert "channel_code" not in _columns(db, "sensor")
