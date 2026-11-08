#!/usr/bin/env python
"""把演示库里的绝对数据路径重定位到安装目录下的 ``data\\四通道保存``。

演示安装包会在 ``data\\四通道保存`` 里带三批现场测试数据；安装到用户选择的目录后，
原库里的 ``D:\\...\\四通道保存\\...`` 路径需要改到 ``{app}\\data\\四通道保存\\...``，
否则工作台能看、原始波形/数据分析页会找不到文件。

用法（安装器自动调用）::

    runtime\\python.exe tools\\relocate_demo.py "C:\\LionMonitor"

没有 ``data\\四通道保存`` 的正式空库安装包调用时直接返回 0，不做任何修改。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

#: 演示数据目录名（安装包里的固定落点）
DATA_DIR_NAME = "四通道保存"


def _relocate_path(text: str, target_root: Path) -> str:
    """把任意包含 ``四通道保存`` 的路径改到 target_root 下，保留批次/文件名后缀。"""
    if not text:
        return text
    parts = Path(text).parts
    if DATA_DIR_NAME not in parts:
        return text
    index = parts.index(DATA_DIR_NAME)
    suffix = parts[index + 1 :]
    return str(target_root.joinpath(*suffix)) if suffix else str(target_root)


def relocate(db_path: Path, project_root: Path) -> int:
    """原地更新 ``dataset.source_dir`` 与 ``channel.source_path``；返回改动条数。"""
    target_root = project_root / "data" / DATA_DIR_NAME
    if not db_path.is_file() or not target_root.is_dir():
        return 0

    changed = 0
    conn = sqlite3.connect(db_path)
    try:
        for table, column in (("dataset", "source_dir"), ("channel", "source_path")):
            rows = conn.execute(f"SELECT id, {column} FROM {table}").fetchall()
            for row_id, value in rows:
                new_value = _relocate_path(str(value or ""), target_root)
                if new_value != value:
                    conn.execute(
                        f"UPDATE {table} SET {column} = ? WHERE id = ?",
                        (new_value, row_id),
                    )
                    changed += 1
        conn.commit()
    finally:
        conn.close()
    return changed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parents[1]
    changed = relocate(root / "data" / "lion.db", root)
    print(f"演示数据路径重定位：{changed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
