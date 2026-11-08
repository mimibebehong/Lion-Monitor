"""数据接入页：网页端目录浏览器（马哥 2026-11-04「不要没反应」）。

覆盖：按钮能打开、能逐级进入、能取消、能「保存」写回并记住、坏路径有明确提示。
原生 Windows 对话框在多人访问 / 服务自启下不可用，页面已不再调用它。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for extra in (ROOT / "src", APP):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import store_ctx  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from lion_monitor.features import ui_settings  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("LION_DB", str(path))
    monkeypatch.setenv(ui_settings.SETTINGS_ENV, str(tmp_path / "ui_settings.json"))
    store_ctx._session_factory.clear()
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        tasks.create_task(session, name="接入任务")
    # 造两个可浏览的兄弟目录
    (tmp_path / "scan" / "batchA").mkdir(parents=True)
    (tmp_path / "scan" / "batchB").mkdir()
    yield tmp_path
    store_ctx._session_factory.clear()


def _page() -> AppTest:
    at = AppTest.from_file(str(APP / "pages" / "ingest.py"), default_timeout=60)
    at.run()
    return at


def _labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def _click(at: AppTest, needle: str) -> AppTest:
    for button in at.button:
        if needle in button.label:
            return button.click().run()
    raise AssertionError(f"没找到按钮「{needle}」，当前按钮：{_labels(at)}")


def test_page_has_browse_button(env):
    at = _page()
    assert any("浏览目录" in label for label in _labels(at))


def test_browse_button_opens_inline_browser(env):
    at = _click(_page(), "浏览目录")

    labels = _labels(at)
    assert any("保存" in label for label in labels), labels
    assert any("取消" in label for label in labels), labels
    assert any("上级" in label for label in labels), labels


def test_browser_lists_subdirs_of_current_dir(env):
    at = _page()
    at.session_state["ingest_dir"] = str(env / "scan")
    at.run()
    at = _click(at, "浏览目录")

    labels = _labels(at)
    assert any("batchA" in label for label in labels), labels
    assert any("batchB" in label for label in labels), labels


def test_entering_subdir_changes_browse_path(env):
    at = _page()
    at.session_state["ingest_dir"] = str(env / "scan")
    at.run()
    at = _click(at, "浏览目录")
    at = _click(at, "batchA")

    assert at.session_state["ingest_browse_path"] == str(env / "scan" / "batchA")


def test_cancel_closes_browser_without_touching_dir(env):
    at = _page()
    at.session_state["ingest_dir"] = str(env / "scan")
    at.run()
    before = at.session_state["ingest_dir"]
    at = _click(at, "浏览目录")
    at = _click(at, "取消")

    assert at.session_state["ingest_browse_open"] is False
    assert at.session_state["ingest_dir"] == before
    assert not any("保存" in label for label in _labels(at))


def test_use_this_dir_writes_back_and_remembers(env):
    at = _page()
    at.session_state["ingest_dir"] = str(env / "scan")
    at.run()
    at = _click(at, "浏览目录")
    at = _click(at, "batchA")
    at = _click(at, "保存")

    chosen = str(env / "scan" / "batchA")
    assert at.session_state["ingest_dir"] == chosen
    assert at.session_state["ingest_browse_open"] is False
    # 记住到设置文件：关掉浏览器/重启服务后还能恢复
    assert ui_settings.get_scan_dir() == chosen


def test_remembered_dir_is_used_on_next_visit(env):
    chosen = str(env / "scan" / "batchB")
    ui_settings.remember_scan_dir(chosen)

    at = _page()
    assert at.session_state["ingest_dir"] == chosen


def test_bad_path_shows_warning_instead_of_silence(env):
    """关键：坏路径必须说话，不能「点了没反应」。"""
    at = _page()
    at.session_state["ingest_dir"] = str(env / "scan")
    at.run()
    at = _click(at, "浏览目录")
    at.session_state["ingest_browse_path"] = str(env / "does-not-exist")
    at.run()

    warnings = " ".join(w.value for w in at.warning)
    assert "不存在" in warnings, warnings
    # 仍然可以退回
    assert any("上级" in label for label in _labels(at))


def test_empty_dir_says_so(env):
    empty = env / "scan" / "batchA"
    at = _page()
    at.session_state["ingest_dir"] = str(empty)
    at.run()
    at = _click(at, "浏览目录")

    captions = " ".join(c.value for c in at.caption)
    assert "没有子文件夹" in captions, captions


def test_subdirs_are_laid_out_in_a_grid_not_one_per_row():
    """马哥 2026-11-05：文件夹不要一个占一行，多个文件夹排一行。"""
    from pathlib import Path as _Path

    src = (_Path(__file__).resolve().parents[1] / "app" / "pages" / "ingest.py").read_text(
        encoding="utf-8"
    )

    assert "DIRS_PER_ROW" in src, "目录网格列数应当提成常量，方便调"
    # 必须按行分批 + 用 columns 排（不是一个 entry 一个 st.button 独占整行）
    assert "for start in range(0, len(entries), DIRS_PER_ROW)" in src
    assert "st.columns(DIRS_PER_ROW" in src
