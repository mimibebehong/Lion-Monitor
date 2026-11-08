"""全库备份（T2）：在线备份 + 保留策略；迁移前自动备份也走这里。

为什么不用 ``cp``：SQLite 在 WAL 模式下新数据还在 ``-wal`` 里，直接复制主体文件会拿到
「半截库」或者不一致快照。``sqlite3.Connection.backup()`` 是官方提供的在线备份 API
（与 CLI 的 ``.backup`` 同源），读写并发时也能拿到一致快照。

目录口径：默认 ``<库文件所在目录>/backups/``，可用 ``LION_BACKUP_DIR`` 覆盖。
文件名：``lion-YYYYmmdd-HHMMSS.db``；同一秒内多次备份自动加 ``-2`` ``-3`` 序号，不互相覆盖。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = [
    "BACKUP_DIRNAME",
    "DEFAULT_KEEP",
    "BackupError",
    "BackupInfo",
    "backup_database",
    "human_size",
    "latest_backup",
    "list_backups",
    "prune_backups",
    "resolve_backup_dir",
]

_log = logging.getLogger(__name__)

#: 默认保留份数（N 份，最新的留下）
DEFAULT_KEEP = 10

#: 备份目录名（库文件同级的 backups/）
BACKUP_DIRNAME = "backups"

#: 备份文件名：lion-YYYYmmdd-HHMMSS.db，同秒冲突时 lion-…-2.db
_NAME_RE = re.compile(r"^lion-(\d{8})-(\d{6})(?:-(\d+))?\.db$")

#: 备份时读库的等待上限（秒），与 db.BUSY_TIMEOUT_S 同口径
_BUSY_TIMEOUT_S = 30


class BackupError(RuntimeError):
    """备份失败（消息一律中文，直接给操作员看）。"""


def human_size(size: int) -> str:
    """人类可读大小（KB / MB）。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.1f} KB"


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """一份备份文件。"""

    path: Path
    size: int
    created_at: datetime

    def size_text(self) -> str:
        """人类可读大小（KB / MB）。"""
        return human_size(self.size)


def resolve_backup_dir(db_path: str | Path, override: str | Path | None = None) -> Path:
    """定备份目录：显式参数 > ``LION_BACKUP_DIR`` > ``<库文件目录>/backups``。"""
    if override is None:
        text = (os.environ.get("LION_BACKUP_DIR") or "").strip()
    else:
        text = str(override).strip()
    if text:
        return Path(text).expanduser()
    return Path(db_path).expanduser().absolute().parent / BACKUP_DIRNAME


def _sort_key(info: BackupInfo) -> tuple:
    """新的在前：先按写入时间（纳秒），同一时间戳再按文件名里的序号倒序。"""
    match = _NAME_RE.match(info.path.name)
    counter = int(match.group(3)) if match and match.group(3) else 1
    return (info.path.stat().st_mtime_ns, counter)


def list_backups(backup_dir: str | Path) -> list[BackupInfo]:
    """列备份文件，最新的在前；目录不存在/没有备份则返回空列表。"""
    folder = Path(backup_dir).expanduser()
    if not folder.is_dir():
        return []
    items: list[BackupInfo] = []
    for path in folder.glob("lion-*.db"):
        if not path.is_file() or not _NAME_RE.match(path.name):
            continue
        stat = path.stat()
        items.append(
            BackupInfo(
                path=path,
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return sorted(items, key=_sort_key, reverse=True)


def latest_backup(db_path: str | Path, backup_dir: str | Path | None = None) -> BackupInfo | None:
    """最近一次备份（没有则 None）。"""
    rows = list_backups(resolve_backup_dir(db_path, backup_dir))
    return rows[0] if rows else None


def prune_backups(backup_dir: str | Path, keep: int = DEFAULT_KEEP) -> list[Path]:
    """只留最新 ``keep`` 份，返回被删掉的文件。"""
    if keep < 1:
        raise BackupError("保留份数必须 ≥ 1")
    rows = list_backups(backup_dir)
    removed: list[Path] = []
    for stale in rows[keep:]:
        stale.path.unlink(missing_ok=True)
        removed.append(stale.path)
    if removed:
        _log.info("备份保留策略：删掉 %d 份旧备份，保留最近 %d 份", len(removed), keep)
    return removed


def _free_name(folder: Path, moment: datetime) -> Path:
    """定备份文件名；同一秒内重复备份加序号，绝不覆盖已有备份。"""
    base = f"lion-{moment:%Y%m%d-%H%M%S}"
    candidate = folder / f"{base}.db"
    index = 2
    while candidate.exists():
        candidate = folder / f"{base}-{index}.db"
        index += 1
    return candidate


def backup_database(
    db_path: str | Path,
    backup_dir: str | Path | None = None,
    keep: int = DEFAULT_KEEP,
) -> BackupInfo:
    """在线备份整个库，返回备份文件信息；顺手按 ``keep`` 清理旧备份。

    失败一律抛 :class:`BackupError`（中文），不会留下半截备份文件。
    """
    if keep < 1:
        raise BackupError("保留份数必须 ≥ 1")
    source_path = Path(db_path).expanduser()
    if not source_path.is_file():
        raise BackupError(f"库文件不存在：{source_path}")

    folder = resolve_backup_dir(source_path, backup_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"备份目录不可写：{folder}（{exc}）") from exc
    if not os.access(folder, os.W_OK):
        raise BackupError(f"备份目录不可写：{folder}")

    dest = _free_name(folder, datetime.now())
    try:
        with (
            closing(sqlite3.connect(str(source_path), timeout=_BUSY_TIMEOUT_S)) as source,
            closing(sqlite3.connect(str(dest), timeout=_BUSY_TIMEOUT_S)) as target,
        ):
            source.backup(target)
    except (sqlite3.Error, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise BackupError(f"在线备份失败：{source_path} → {dest}（{exc}）") from exc

    info = BackupInfo(
        path=dest,
        size=dest.stat().st_size,
        created_at=datetime.fromtimestamp(dest.stat().st_mtime),
    )
    try:
        with closing(sqlite3.connect(str(dest))) as conn:
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:  # pragma: no cover - 极端盘故障
        dest.unlink(missing_ok=True)
        raise BackupError(f"备份文件校验失败：{dest}（{exc}）") from exc
    if not tables or str(check).lower() != "ok":
        dest.unlink(missing_ok=True)
        raise BackupError(f"备份文件校验失败：{dest}（表 {tables} 张，quick_check={check}）")

    _log.info("数据库备份完成：%s（%s，保留最近 %d 份）", info.path, info.size_text(), keep)
    prune_backups(folder, keep)
    return info
