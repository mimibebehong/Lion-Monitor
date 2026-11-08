"""装配唯一性按 ref_id 判定（马哥 2026-11-02）；名字只是显示缓存，不能拿来挡重复。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import channel_reg, metric_def, object_registry, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.tree import TreeError  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "unique_ref.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="唯一装配")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        device2 = tree.create_node(session, task.id, "device", "产线2", parent_id=root.id)
        obj = object_registry.create_object(session, task.id, "辊压机", "轴承")
        chan = channel_reg.create_channel(session, task.id, obj.id, "CH1")
        metric_def.seed_defaults(session, task.id)
        metric = metric_def.list_metrics(session, task.id)[0]
        ids = {
            "task": task.id,
            "device": device.id,
            "device2": device2.id,
            "object": obj.id,
            "channel_row": chan.id,
            "metric_def": metric.id,
            "metric_code": metric.code,
        }
    yield {"factory": factory, **ids}


def _component(session, env, parent_id, name="辊压机"):
    return tree.create_node(
        session,
        env["task"],
        "component",
        name,
        parent_id=parent_id,
        ref_type="monitor_object",
        ref_id=env["object"],
    )


def test_same_object_cannot_be_assembled_twice(env):
    with env["factory"]() as session:
        _component(session, env, env["device"])
    with env["factory"]() as session, pytest.raises(TreeError, match="已经装配"):
        _component(session, env, env["device"], name="另一个名字")


def test_same_object_cannot_be_assembled_under_another_device(env):
    with env["factory"]() as session:
        _component(session, env, env["device"])
    with env["factory"]() as session, pytest.raises(TreeError, match="已经装配"):
        _component(session, env, env["device2"])


def test_same_channel_can_be_assembled_under_several_components(env):
    """同一台传感器通道可以挂到多个监测对象下（马哥 2026-11-02 口径）。"""
    with env["factory"]() as session:
        comp = _component(session, env, env["device"])
        tree.create_node(
            session,
            env["task"],
            "channel",
            "CH1",
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=env["channel_row"],
        )
    with env["factory"]() as session:
        comp2 = tree.create_node(
            session,
            env["task"],
            "component",
            "另一台",
            parent_id=env["device"],
            ref_type=None,
            ref_id=None,
        )
        tree.create_node(
            session,
            env["task"],
            "channel",
            "CH1",
            parent_id=comp2.id,
            ref_type="channel_reg",
            ref_id=env["channel_row"],
        )
        # 同一个父节点下仍然不许重复
        with pytest.raises(TreeError, match="已经装配"):
            tree.create_node(
                session,
                env["task"],
                "channel",
                "CH1",
                parent_id=comp2.id,
                ref_type="channel_reg",
                ref_id=env["channel_row"],
            )


def test_same_metric_def_allowed_on_different_channels(env):
    with env["factory"]() as session:
        comp = _component(session, env, env["device"])
        ch1 = tree.create_node(
            session,
            env["task"],
            "channel",
            "CH1",
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=env["channel_row"],
        )
        ch2 = tree.create_node(session, env["task"], "channel", "CH2", parent_id=comp.id)
        for parent in (ch1, ch2):
            tree.create_node(
                session,
                env["task"],
                "metric",
                env["metric_code"],
                parent_id=parent.id,
                attrs={"code": env["metric_code"]},
                ref_type="metric_def",
                ref_id=env["metric_def"],
            )
        with pytest.raises(TreeError, match="已经装配"):
            tree.create_node(
                session,
                env["task"],
                "metric",
                env["metric_code"],
                parent_id=ch1.id,
                attrs={"code": env["metric_code"]},
                ref_type="metric_def",
                ref_id=env["metric_def"],
            )
