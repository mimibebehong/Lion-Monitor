"""演示安装包的数据路径重定位。"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("relocate_demo", ROOT / "tools" / "relocate_demo.py")
assert _SPEC and _SPEC.loader
relocate_demo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(relocate_demo)


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "lion.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE dataset (id INTEGER PRIMARY KEY, source_dir TEXT)")
        conn.execute("CREATE TABLE channel (id INTEGER PRIMARY KEY, source_path TEXT)")
        conn.execute(
            "INSERT INTO dataset VALUES (1, ?)",
            (r"D:\code_python\Project\Lion-Monitor\data\四通道保存\2026-10-29 15_28_34",),
        )
        conn.execute(
            "INSERT INTO channel VALUES (1, ?)",
            (r"D:\code_python\Project\Lion-Monitor\data\四通道保存\2026-10-29 15_28_34\CH1.dat",),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_relocate_rewrites_paths_to_install_root(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    target = tmp_path / "data" / "四通道保存" / "2026-10-29 15_28_34"
    target.mkdir(parents=True)
    (target / "CH1.dat").write_text("x", encoding="utf-8")

    changed = relocate_demo.relocate(db, tmp_path)

    assert changed == 2
    with sqlite3.connect(db) as conn:
        source_dir = conn.execute("SELECT source_dir FROM dataset").fetchone()[0]
        source_path = conn.execute("SELECT source_path FROM channel").fetchone()[0]
    assert source_dir == str(target)
    assert source_path == str(target / "CH1.dat")


def test_relocate_is_noop_without_demo_data(tmp_path: Path) -> None:
    db = _make_db(tmp_path)

    assert relocate_demo.relocate(db, tmp_path) == 0
    with sqlite3.connect(db) as conn:
        source_dir = conn.execute("SELECT source_dir FROM dataset").fetchone()[0]
    assert source_dir.startswith(r"D:\code_python")


def test_relocate_keeps_unrelated_paths(tmp_path: Path) -> None:
    db = tmp_path / "data" / "lion.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE dataset (id INTEGER PRIMARY KEY, source_dir TEXT)")
        conn.execute("CREATE TABLE channel (id INTEGER PRIMARY KEY, source_path TEXT)")
        conn.execute("INSERT INTO dataset VALUES (1, ?)", (r"D:\other\data",))
        conn.execute("INSERT INTO channel VALUES (1, ?)", (r"D:\other\data\CH1.dat",))
        conn.commit()
    finally:
        conn.close()
    (tmp_path / "data" / "四通道保存").mkdir()

    assert relocate_demo.relocate(db, tmp_path) == 0
