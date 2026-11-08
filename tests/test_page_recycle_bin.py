"""回收站页面（P6-3 / G6）：节点级留痕要能在页面上看到路径与剩余保留天数。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
RECYCLE_PAGE = ROOT / "app" / "pages" / "recycle_bin.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402


@pytest.fixture()
def db_with_deleted_node(tmp_path, monkeypatch):
    path = tmp_path / "recycle_page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="卷绕产线振动监测")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        component = tree.create_node(
            session, task.id, "component", "隔膜过辊轴承", parent_id=device.id
        )
        tree.create_node(session, task.id, "channel", "CH1", parent_id=component.id)
        tree.soft_delete_node(session, component.id, operator="小智")
    engine.dispose()
    return path


def _texts(at: AppTest) -> str:
    parts: list[str] = []
    for group in (at.markdown, at.caption, at.success, at.info, at.warning, at.error):
        parts.extend(str(item.value) for item in group)
    return " ".join(parts)


def test_recycle_page_lists_node_path_with_remaining_days(db_with_deleted_node):
    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存需按本次库路径重建）

    store_ctx._session_factory.clear()

    at = AppTest.from_file(str(RECYCLE_PAGE), default_timeout=120).run()

    assert [exc.value for exc in at.exception] == []
    frame = at.dataframe[0].value
    assert "剩余天数" in list(frame.columns)
    assert [str(value) for value in frame["类型"]] == ["节点"]
    assert "隔膜过辊轴承" in str(frame["名称"].iloc[0])
    assert str(frame["名称"].iloc[0]) == "卷绕产线振动监测 / 卷绕产线 / 隔膜过辊轴承"
    assert str(frame["级联"].iloc[0]) == "节点 2"
    assert str(frame["剩余天数"].iloc[0]) == "7 天"
    assert "保留期" in _texts(at)

    store_ctx._session_factory.clear()
