"""通道下装配指标节点时自动建参数卡（马哥 2026-11-02：不用再点批量绑定）。"""

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
from lion_monitor.store import metric_assign, metric_def, tasks, tree  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "autobind.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="自动建卡")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "辊压机", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric_def.seed_defaults(session, task.id)
        ids = {"task": task.id, "channel": channel.id}
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"engine": engine, "factory": factory, **ids}
    store_ctx._session_factory.clear()


def test_adding_metric_node_creates_params_card(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = env["channel"]
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    picker = next(item for item in at.selectbox if "指标（来自台账）" in item.label)
    option = next(text for text in picker.options if "v_RMS" in text)
    picker.set_value(option).run()
    next(button for button in at.button if button.label == "添加到树上").click().run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    with env["factory"]() as session:
        metric_nodes = [
            node for node in tree.list_nodes(session, env["task"]) if node.node_type == "metric"
        ]
        rows = metric_assign.list_assignments(session, env["task"])
    assert len(metric_nodes) == 1
    assert [row.node_id for row in rows] == [metric_nodes[0].id]
    assert rows[0].code == "v_RMS"
    assert rows[0].params == {}


def test_channel_still_needs_registered_channel(env):
    """通道下没有指标时不报错，只提示（防止自动建卡逻辑把空通道搞崩）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = env["channel"]
    at.run()

    assert [exc.value for exc in at.exception] == []
    with env["factory"]() as session:
        assert metric_assign.list_assignments(session, env["task"]) == []
