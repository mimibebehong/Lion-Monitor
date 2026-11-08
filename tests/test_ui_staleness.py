"""开发期提醒：源码比服务进程新时，在页面顶部提示「请重启服务」。

背景：Streamlit 只重跑页面脚本，不会重新导入已加载的模块。看板 worker 提交新代码后，
运行中的进程仍用旧模块，页面会报 `ImportError: cannot import name ...` /
`AttributeError: ... has no attribute ...` 这类假报错（symbol 在源码里明明存在）。
这里只做提示，不自动重启。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.ui_staleness import (  # noqa: E402
    newest_source_mtime,
    process_started_at,
    stale_notice,
)


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    import os

    os.utime(path, (mtime, mtime))
    return path


def test_newest_source_mtime_picks_latest_py_file(tmp_path):
    _touch(tmp_path / "a.py", 1000.0)
    _touch(tmp_path / "pkg" / "b.py", 3000.0)
    _touch(tmp_path / "c.txt", 5000.0)  # 非 .py 不参与
    assert newest_source_mtime(tmp_path) == pytest.approx(3000.0)


def test_newest_source_mtime_none_when_no_sources(tmp_path):
    _touch(tmp_path / "readme.md", 1000.0)
    assert newest_source_mtime(tmp_path) is None


def test_stale_notice_silent_when_sources_older_than_process():
    assert stale_notice(started_at=2000.0, newest_mtime=1000.0) is None


def test_stale_notice_silent_within_startup_tolerance():
    # 进程启动瞬间正在被写入的文件，不算「代码更新」
    assert stale_notice(started_at=2000.0, newest_mtime=2001.0) is None


def test_stale_notice_mentions_both_times_and_restart():
    notice = stale_notice(started_at=2000.0, newest_mtime=4000.0)
    assert notice is not None
    assert "重启" in notice
    started = time.strftime("%H:%M:%S", time.localtime(2000.0))
    changed = time.strftime("%H:%M:%S", time.localtime(4000.0))
    assert started in notice and changed in notice


def test_stale_notice_silent_when_unknown():
    assert stale_notice(started_at=None, newest_mtime=4000.0) is None
    assert stale_notice(started_at=2000.0, newest_mtime=None) is None


def test_process_started_at_is_a_past_wallclock_time():
    started = process_started_at()
    assert started is not None
    now = time.time()
    assert 0 < started <= now + 1.0
    assert started > now - 7 * 24 * 3600  # 不会给出离谱的远古时间
