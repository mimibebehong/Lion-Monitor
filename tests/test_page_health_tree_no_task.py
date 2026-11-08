"""Q5：空库打开健康树页 → 不自动播种，只给「恢复内置示例树」按钮；点了才建示例树。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "health_tree.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402


@pytest.fixture()
def empty_db(tmp_path, monkeypatch):
    path = tmp_path / "no_task.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    return path


def test_page_offers_example_tree_button_when_no_task(empty_db):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()

    assert [exc.value for exc in at.exception] == []
    assert any("还没有测试任务" in item.value for item in at.info), [m.value for m in at.info]
    labels = [button.label for button in at.button]
    assert "恢复内置示例树" in labels, labels
    # 打开页面不会自己造任务
    with store_ctx.open_session() as session:
        assert tasks.list_tasks(session) == []


def test_clicking_button_builds_example_task_with_21_nodes(empty_db):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()
    at.button(key="ht_reset_example").click().run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    with store_ctx.open_session() as session:
        rows = tasks.list_tasks(session)
        assert len(rows) == 1
        assert rows[0].name == "卷绕产线振动监测"
        assert len(tree.list_nodes(session, rows[0].id)) == 21

    store_ctx._session_factory.clear()
