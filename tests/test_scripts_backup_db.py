"""T2 命令行：scripts/backup_db.py 真跑（--db / --backup-dir / --keep）。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_db.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


def _seed(db_path: Path, count: int = 2) -> Path:
    engine = store_db.make_engine(db_path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="命令行备份任务")
        for idx in range(count):
            datasets.create_dataset(
                session, task.id, f"批次{idx}", f"/data/cli{idx}", "2026-11-02 11:00:00"
            )
    engine.dispose()
    return db_path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        check=False,
    )


def test_cli_writes_backup_and_reports_path_and_size(tmp_path):
    db = _seed(tmp_path / "lion.db", count=2)
    folder = tmp_path / "bk"

    result = _run("--db", str(db), "--backup-dir", str(folder), "--keep", "3")

    assert result.returncode == 0, result.stderr
    files = sorted(folder.glob("lion-*.db"))
    assert len(files) == 1
    assert str(files[0]) in result.stdout
    assert "字节" in result.stdout and "KB" in result.stdout
    with sqlite3.connect(files[0]) as conn:
        assert conn.execute("select count(*) from dataset").fetchone()[0] == 2


def test_cli_keep_prunes_to_n_files(tmp_path):
    db = _seed(tmp_path / "lion.db", count=1)
    folder = tmp_path / "bk"

    for _ in range(12):
        result = _run("--db", str(db), "--backup-dir", str(folder), "--keep", "3")
        assert result.returncode == 0, result.stderr

    assert len(sorted(folder.glob("lion-*.db"))) == 3


def test_cli_missing_db_exits_with_chinese_error(tmp_path):
    result = _run("--db", str(tmp_path / "none.db"), "--backup-dir", str(tmp_path / "bk"))

    assert result.returncode == 1
    assert "备份失败" in result.stderr
    assert "库文件不存在" in result.stderr
