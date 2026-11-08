"""目录浏览器：只列子目录、能上下走、失败给人话（马哥 2026-11-04「不要没反应」）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lion_monitor.features.dir_browser import (  # noqa: E402
    DirError,
    drives,
    list_subdirs,
    parent_of,
    validate_dir,
)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """tmp_path 下造：目录 a/、B/、.hidden/，以及文件 notes.txt。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a" / "inner").mkdir()
    return tmp_path


def test_lists_only_directories(tree: Path):
    names = [e.name for e in list_subdirs(tree)]
    assert "notes.txt" not in names
    assert set(names) == {"a", "B"}


def test_sorted_case_insensitively(tree: Path):
    names = [e.name for e in list_subdirs(tree)]
    assert names == sorted(names, key=str.lower)


def test_hidden_filtered_by_default_but_optional(tree: Path):
    assert ".hidden" not in [e.name for e in list_subdirs(tree)]
    assert ".hidden" in [e.name for e in list_subdirs(tree, show_hidden=True)]


def test_entries_carry_absolute_paths(tree: Path):
    entry = next(e for e in list_subdirs(tree) if e.name == "a")
    assert Path(entry.path) == tree / "a"


def test_missing_path_raises_readable_error(tmp_path: Path):
    with pytest.raises(DirError) as excinfo:
        list_subdirs(tmp_path / "nope")
    assert "不存在" in str(excinfo.value)


def test_file_path_rejected(tree: Path):
    with pytest.raises(DirError) as excinfo:
        list_subdirs(tree / "notes.txt")
    assert "不是文件夹" in str(excinfo.value)


def test_empty_dir_returns_empty_list_not_error(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_subdirs(empty) == []


def test_timeout_guard_raises_readable_error(tmp_path: Path, monkeypatch):
    import lion_monitor.features.dir_browser as mod

    def slow_scan(path, show_hidden):
        import time

        time.sleep(1.0)
        return []

    monkeypatch.setattr(mod, "_scan", slow_scan)
    with pytest.raises(DirError) as excinfo:
        list_subdirs(tmp_path, timeout_s=0.05)
    assert "超时" in str(excinfo.value)


def test_parent_walks_up_and_stops_at_root(tree: Path):
    child = tree / "a" / "inner"
    up = parent_of(child)
    assert up is not None
    assert Path(up) == tree / "a"
    assert parent_of(tree) == str(tree.parent)


def test_parent_of_root_is_none():
    root = "C:\\" if sys.platform == "win32" else "/"
    assert parent_of(root) is None


def test_drives_non_empty_and_valid():
    found = drives()
    assert found, "至少要有一个可用根"
    for label, path in found:
        assert label and path


def test_validate_dir_ok_for_directory(tree: Path):
    assert validate_dir(tree) is None


def test_validate_dir_reports_blank_and_missing(tmp_path: Path):
    blank = validate_dir("")
    ghost = validate_dir(tmp_path / "ghost")
    assert blank is not None and "请先选择" in blank
    assert ghost is not None and "不存在" in ghost


def test_validate_dir_rejects_file(tree: Path):
    not_dir = validate_dir(tree / "notes.txt")
    assert not_dir is not None and "不是文件夹" in not_dir


@pytest.mark.skipif(sys.platform == "win32", reason="UNIX 权限位才可控")
def test_permission_error_message_mentions_unc_hint(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root 无视权限位")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(DirError) as excinfo:
            list_subdirs(locked)
        msg = str(excinfo.value)
        assert "无权访问" in msg and "server" in msg
    finally:
        locked.chmod(0o755)
