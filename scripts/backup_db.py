#!/usr/bin/env python
"""整库备份命令行（T2）。

用 ``sqlite3.Connection.backup()`` 在线备份，不用 ``cp``（WAL 模式下直接复制会拿到不一致快照）。

用法::

    .venv/bin/python scripts/backup_db.py                      # data/lion.db → data/backups/
    .venv/bin/python scripts/backup_db.py --keep 3             # 只留最近 3 份
    .venv/bin/python scripts/backup_db.py --db /path/lion.db --backup-dir /path/bk

退出码：0 = 备份成功；1 = 失败（stderr 给中文原因）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store.backup import (  # noqa: E402
    DEFAULT_KEEP,
    BackupError,
    backup_database,
)
from lion_monitor.store.db import DEFAULT_DB_PATH  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lion-Monitor 整库在线备份（sqlite3 backup API）",
    )
    parser.add_argument(
        "--db",
        default=str(ROOT / DEFAULT_DB_PATH),
        help=f"要备份的库文件（默认 {DEFAULT_DB_PATH}）",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="备份目录（默认 <库文件目录>/backups，也可用 LION_BACKUP_DIR 指定）",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"保留最近几份备份（默认 {DEFAULT_KEEP}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        info = backup_database(args.db, args.backup_dir or None, keep=args.keep)
    except BackupError as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1
    print(f"备份完成：{info.path}")
    print(f"备份大小：{info.size} 字节（{info.size_text()}）")
    print(f"备份时间：{info.created_at:%Y-%m-%d %H:%M:%S}")
    print(f"保留份数：最近 {args.keep} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
