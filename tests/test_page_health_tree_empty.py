"""健康树页空态：任务下没有任何节点时给空态提示，不回落演示数据。

用 Streamlit ``AppTest`` 真跑页面（不是静态断言文案），库指向临时 SQLite。
"""

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
def db_without_tree(tmp_path, monkeypatch):
    """建一个任务并把它的健康树整棵软删 —— 库里有数据，但当前任务没有树。"""
    path = tmp_path / "empty_tree.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="空树任务", operator="mark")
        root = tree.root_node(session, task.id)
        tree.soft_delete_node(session, root.id)
    return path


def test_health_tree_page_shows_empty_state_without_falling_back_to_demo(db_without_tree):
    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()

    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()

    assert [exc.value for exc in at.exception] == []
    infos = [item.value for item in at.info]
    assert any("还没有健康树" in text for text in infos), infos
    # 树没渲染出来：连节点树区块都没有
    assert not any(item.value.startswith("#### 节点树") for item in at.markdown)

    store_ctx._session_factory.clear()
