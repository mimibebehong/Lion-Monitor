"""Q5（G7）：空库不再自动播种示例任务，播种改成健康树页的显式按钮。

背景（``docs/reviews/2026-10-31-总体复查.md`` G7）：``app/store_ctx.py`` 在首次建会话
工厂时调 ``bootstrap_example_task``，现场空库一打开就混进 21 节点示例树。
现在：空库 = 0 任务；要示例树得自己点「恢复内置示例树」。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
SRC_DIR = ROOT / "src"
for extra in (SRC_DIR, APP_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import tasks  # noqa: E402


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    monkeypatch.setenv("LION_DB", str(path))
    import store_ctx  # noqa: E402

    store_ctx._session_factory.clear()
    yield path
    store_ctx._session_factory.clear()


def _count(path: Path, table: str, where: str = "") -> int:
    clause = f" WHERE {where}" if where else ""
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}{clause}").fetchone()[0]


def test_first_open_of_empty_db_creates_no_task(empty_db):
    import store_ctx  # noqa: E402

    with store_ctx.open_session() as session:
        assert tasks.list_tasks(session) == []

    assert _count(empty_db, "test_task") == 0
    assert _count(empty_db, "task_node") == 0


def test_bootstrap_function_still_available_for_explicit_use(empty_db):
    """函数保留（健康树页的显式按钮用它），只是不再被隐式调用。"""
    import store_ctx  # noqa: E402

    from lion_monitor.store.bootstrap import bootstrap_example_task

    with store_ctx.open_session() as session:
        row = bootstrap_example_task(session)

    assert row is not None
    assert _count(empty_db, "test_task") == 1
    # 导入整树会软删旧根节点，所以按「未删除」数 21 个示例节点
    assert _count(empty_db, "task_node", "deleted_at IS NULL") == 21
