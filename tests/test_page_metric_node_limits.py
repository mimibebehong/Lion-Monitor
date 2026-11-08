"""指标节点内联阈值（马哥 2026-11-02：阈值跟着指标走，通道/对象级做批量）。"""

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
    path = tmp_path / "metric_limits.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="指标阈值")
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
            attrs={"code": "v_RMS", "单位": "mm/s"},
        )
        metric_def.seed_defaults(session, task.id)
        metric_assign.assign_metric(session, task.id, metric.id, "v_RMS")
        # 对象级阈值：通道级会被迁移到指标节点，这里用对象级验证"继承"
        limits.set_limit(session, task.id, comp.id, "v_RMS", 4.5, 7.1, ">")
        ids = {"task": task.id, "channel": channel.id, "metric": metric.id}
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, **ids}
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = env["metric"]
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _number(at, label):
    for item in at.number_input:
        if item.label.startswith(label):
            return item
    raise AssertionError(f"没找到 {label}；现有 {[i.label for i in at.number_input]}")


def _button(at, label):
    for item in at.button:
        if item.label == label:
            return item
    raise AssertionError(f"没找到按钮 {label}；现有 {[b.label for b in at.button]}")


def test_metric_node_shows_inherited_limit(env):
    at = _app(env)
    assert _number(at, "warn").value == pytest.approx(4.5)
    assert _number(at, "alert").value == pytest.approx(7.1)
    captions = " ".join(str(item.value) for item in at.caption)
    assert "继承自对象级" in captions, captions


def test_saving_limit_writes_metric_node(env):
    at = _app(env)
    _number(at, "warn").set_value(5.0)
    _number(at, "alert").set_value(8.0)
    _button(at, "保存阈值").click().run()

    with env["factory"]() as session:
        rows = [
            row for row in limits.list_limits(session, env["task"]) if row.metric_code == "v_RMS"
        ]
    own = [row for row in rows if row.node_id == env["metric"]]
    assert own, rows
    assert (own[0].warn, own[0].alert) == (5.0, 8.0)
    assert own[0].node_type == "metric"


def test_deleting_own_limit_falls_back_to_inherited(env):
    at = _app(env)
    _button(at, "保存阈值").click().run()  # 先落一条自己的（值等于继承值）
    with env["factory"]() as session:
        own = [
            row
            for row in limits.list_limits(session, env["task"])
            if row.node_id == env["metric"] and row.metric_code == "v_RMS"
        ]
    assert own

    at = _app(env)
    _button(at, "删除阈值（回到继承）").click().run()
    with env["factory"]() as session:
        remaining = [
            row
            for row in limits.list_limits(session, env["task"])
            if row.node_id == env["metric"] and row.metric_code == "v_RMS"
        ]
    assert remaining == []
    captions = " ".join(str(item.value) for item in at.caption)
    assert "继承自对象级" in captions, captions


def _clear_all_limits(env):
    with env["factory"]() as session:
        for row in limits.list_limits(session, env["task"]):
            limits.delete_limit(session, row.id)


def test_inputs_blank_when_no_limit_anywhere(env):
    """马哥 2026-11-03：删掉阈值后输入框不该显示 0.000（0 本身不合法）。"""
    _clear_all_limits(env)
    at = _app(env)

    assert _number(at, "warn").value is None
    assert _number(at, "alert").value is None
    captions = " ".join(str(item.value) for item in at.caption)
    assert "还没有阈值" in captions, captions


def test_saving_blank_limit_asks_to_fill(env):
    _clear_all_limits(env)
    at = _app(env)
    _button(at, "保存阈值").click().run()

    errors = [str(item.value) for item in at.error]
    assert any("先填 warn 和 alert" in text for text in errors), errors
