"""通道页签可直接移除指标节点（现场口径：通道这一级能增删指标节点）。"""

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
from lion_monitor.store import limits, metric_assign, metric_def, tasks, tree  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "remove_metric.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="移除指标")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "辊压机", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric = tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=channel.id,
            attrs={"code": "v_RMS"},
        )
        metric_def.seed_defaults(session, task.id)
        metric_assign.assign_metric(session, task.id, metric.id, "v_RMS")
        limits.set_limit(session, task.id, metric.id, "v_RMS", 1.0, 2.0, ">")
        ids = {"task": task.id, "channel": channel.id, "metric": metric.id}
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, **ids}
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = env["channel"]
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _button(at, label):
    for item in at.button:
        if item.label == label:
            return item
    raise AssertionError(f"没找到按钮 {label}；现有 {[b.label for b in at.button]}")


def test_channel_summary_shows_params_and_limit(env):
    at = _app(env)
    captions = " ".join(str(item.value) for item in at.caption)
    assert "窗长" in captions
    assert "阈值 1/2" in captions, captions


def test_removing_metric_node_from_channel_summary(env):
    at = _app(env)
    _button(at, "移除").click().run()
    assert any("确认移除" == button.label for button in at.button), [b.label for b in at.button]
    _button(at, "确认移除").click().run()

    with env["factory"]() as session:
        names = [node.name for node in tree.list_nodes(session, env["task"])]
        assigns = metric_assign.list_assignments(session, env["task"])
        limits_rows = limits.list_limits(session, env["task"])
        recycled = [
            item for item in tree.list_nodes(session, env["task"]) if item.deleted_at is not None
        ]
    assert "v_RMS" not in names
    assert assigns == []
    assert limits_rows == []
    assert recycled == []  # list_nodes 只返回未删的，软删的走回收站
