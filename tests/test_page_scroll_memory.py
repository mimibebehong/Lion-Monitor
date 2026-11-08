"""全局滚动位置记忆（马哥 2026-11-03：点任何按钮刷新后页面位置要保持，要全局记住）。

实现放在 ``ui.page_header()``，所有页面自动生效；这里只验证脚本内容与被注入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGES = ROOT / "app" / "pages"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import ui  # noqa: E402

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "scroll.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        tasks.create_task(session, name="滚动记忆")
    import store_ctx

    store_ctx._session_factory.clear()
    yield path
    store_ctx._session_factory.clear()


def test_script_remembers_and_restores_scroll():
    html = ui.SCROLL_MEMORY_HTML
    assert "stMain" in html, "必须作用在 stMain（主区域）上"
    assert "scrollTop" in html and "localStorage" in html
    assert "ui_scroll:" in html, "按页面路径分别记忆"
    assert "location.pathname" in html


@pytest.mark.parametrize("page", ["tasks.py", "health_tree.py", "trends.py"])
def test_every_page_injects_scroll_script(env, page):
    """page_header 里注入 → 所有页面都有，不用每页自己写。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGES / page), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert len(at.get("iframe")) >= 1, "滚动记忆必须先渲染一个脚本 iframe"


def test_script_ignores_scroll_reset_during_restore_window():
    """重跑把 scrollTop 清零时会触发 scroll 事件；还原窗口内不许把 0 存进去。

    马哥 2026-11-03：重算完成后页面跳回顶部 —— 就是清零事件把记忆值覆盖成 0 造成的。
    """
    html = ui.SCROLL_MEMORY_HTML
    assert "__uiScrollUntil" in html, "要用「还原窗口」标记，重跑期间不写记忆值"
    assert "pointerdown" in html, "点击时先记下目标位置"
    assert "wheel" in html and "cancel" in html, "用户主动滚动时要放弃还原，不能抢滚动"
    assert "setTimeout" in html, "还原走「每次执行重试」，不用常驻定时器（会随 iframe 销毁）"
    assert '"drop"' in html, "拖拽上传不走 pointerdown，要单独记位置"
    assert "setTimeout" in html, "还原走「每次执行重试」，不用常驻定时器（会随 iframe 销毁）"
    assert "__uiScrollHandlers" in html, (
        "监听器要存引用并在每次执行时重挂（iframe 重建会带走旧监听）"
    )


def test_script_remembers_tab_selection():
    """有 tab 的页面，重跑后要回到原 tab（马哥 2026-11-03）。"""
    html = ui.SCROLL_MEMORY_HTML
    assert 'data-testid="stTabs"' in html, "要按 tab 组找到页签"
    assert "ui_tab:" in html, "tab 选中态按页面路径分别记忆"
    assert "aria-selected" in html, "只有当前不在该 tab 时才切回去"
    assert "restoreTabs" in html and "hit.click()" in html
    assert "st-key-" in html, "带 key 的 tab 组（健康树节点设置）按 key 记，不按顺序"
