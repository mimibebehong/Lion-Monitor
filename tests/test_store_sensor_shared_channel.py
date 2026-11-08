"""同一台传感器通道挂到多个监测对象下：两个节点都解析到同一台传感器。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import channel_reg, object_registry, sensors, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "shared_channel.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="共用传感器")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        obj_a = object_registry.create_object(session, task.id, "入片牵引电机", "电机")
        obj_b = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承")
        comp_a = tree.create_node(
            session,
            task.id,
            "component",
            obj_a.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj_a.id,
        )
        comp_b = tree.create_node(
            session,
            task.id,
            "component",
            obj_b.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj_b.id,
        )
        row = channel_reg.create_channel(session, task.id, obj_a.id, "CH3")
        node_a = tree.create_node(
            session,
            task.id,
            "channel",
            row.code,
            parent_id=comp_a.id,
            ref_type="channel_reg",
            ref_id=row.id,
        )
        node_b = tree.create_node(
            session,
            task.id,
            "channel",
            row.code,
            parent_id=comp_b.id,
            ref_type="channel_reg",
            ref_id=row.id,
        )
        created = sensors.create_sensor(
            session, task.id, node_a.id, model="IEPE 500mV/g", sensitivity="500 mV/g"
        )
        ids = {"node_a": node_a.id, "node_b": node_b.id, "sensor": created.id}
    yield {"factory": factory, **ids}


def test_both_channel_nodes_show_the_same_sensor(env):
    with env["factory"]() as session:
        first = sensors.sensor_of_node(session, env["node_a"])
        second = sensors.sensor_of_node(session, env["node_b"])
    assert first is not None and second is not None
    assert first.id == second.id == env["sensor"]
    assert first.model == "IEPE 500mV/g"


def test_sensor_count_stays_one(env):
    with env["factory"]() as session:
        task_id = tree.get_node(session, env["node_a"]).task_id
        rows = sensors.list_sensors(session, task_id)
    assert len(rows) == 1
