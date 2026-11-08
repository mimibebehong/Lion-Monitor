"""存储层库文件路径解析：默认落在工程 data/ 下，可用显式路径或环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from lion_monitor.store import db


def test_resolve_db_path_defaults_under_project_root(tmp_path):
    assert db.resolve_db_path(tmp_path) == tmp_path / "data" / "lion.db"


def test_resolve_db_path_prefers_explicit_override(tmp_path):
    target = tmp_path / "custom" / "lion.db"

    assert db.resolve_db_path(tmp_path, str(target)) == target


def test_resolve_db_path_expands_user_home(tmp_path):
    assert db.resolve_db_path(tmp_path, "~/lion.db") == Path.home() / "lion.db"


def test_resolve_db_path_ignores_blank_override(tmp_path):
    assert db.resolve_db_path(tmp_path, "   ") == tmp_path / "data" / "lion.db"
