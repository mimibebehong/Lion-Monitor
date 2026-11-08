"""通道/传感器归属：自由态 ⇄ 绑定任意监测对象（马哥 2026-11-02 口径）。

口径：
- 传感器是台账主体，有「自由」（不归属任何监测对象）与「已绑定」两态；归属是**绑定关系**。
- 自由传感器可以绑到任意监测对象；已绑定也能改绑，也能解绑回自由。
- 解绑时健康树上那个通道节点自动摘除（进回收站，可恢复），台账变自由。
- 自由通道不会出现在任何对象下的「新增通道」池里。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import channel_reg, object_registry, recycle, sensors, tasks, tree
from lion_monitor.store import db as store_db


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.session_scope(store_db.make_session_factory(engine)) as s:
        yield s


@pytest.fixture()
def env(session):
    task = tasks.create_task(session, name="归属任务")
    motor = object_registry.create_object(session, task.id, "入片牵引电机", "电机")
    bearing = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承")
    # 自由通道：没有任何对象归属
    free = channel_reg.create_channel(session, task.id, None, "CH2")
    sensor = sensors.create_sensor(
        session, task.id, node_id=None, model="IEPE 500mV/g", channel_code="CH2"
    )
    return {"task": task, "motor": motor, "bearing": bearing, "free": free, "sensor": sensor}


def test_free_channel_has_no_object(session, env):
    rows = {r.code: r for r in channel_reg.list_channels(session, env["task"].id)}

    assert rows["CH2"].object_id is None
    assert rows["CH2"].object_name == ""


def test_free_channel_never_shows_up_in_any_object_pool(session, env):
    rows = channel_reg.list_channels(session, env["task"].id)

    assert channel_reg.available_for_object(rows, env["motor"].id, used=set()) == []
    assert channel_reg.available_for_object(rows, env["bearing"].id, used=set()) == []


def test_sensor_reports_free_state(session, env):
    row = sensors.list_sensors(session, env["task"].id)[0]

    assert row.is_free
    assert row.object_name == ""


def test_bind_free_sensor_to_any_object(session, env):
    sensors.bind_object(session, env["sensor"].id, env["bearing"].id)

    row = sensors.list_sensors(session, env["task"].id)[0]
    assert (row.object_id, row.object_name, row.is_free) == (
        env["bearing"].id,
        "隔膜过辊轴承",
        False,
    )


def test_rebind_moves_ownership(session, env):
    sensors.bind_object(session, env["sensor"].id, env["bearing"].id)
    sensors.bind_object(session, env["sensor"].id, env["motor"].id)

    row = sensors.list_sensors(session, env["task"].id)[0]
    assert row.object_name == "入片牵引电机"


def test_unbind_removes_channel_node_from_tree(session, env):
    sensors.bind_object(session, env["sensor"].id, env["motor"].id)
    # 装配到健康树
    root = tree.root_node(session, env["task"].id)
    device = tree.create_node(session, env["task"].id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        env["task"].id,
        "component",
        "入片牵引电机",
        parent_id=device.id,
        ref_type="monitor_object",
        ref_id=env["motor"].id,
    )
    ch = channel_reg.list_channels(session, env["task"].id)[0]
    node = tree.create_node(
        session,
        env["task"].id,
        "channel",
        ch.code,
        parent_id=component.id,
        ref_type="channel_reg",
        ref_id=ch.id,
    )
    sensors.attach_node(session, env["sensor"].id, node.id)

    sensors.bind_object(session, env["sensor"].id, None)

    from lion_monitor.store.models import TaskNode

    assert session.get(TaskNode, node.id).deleted_at is not None
    row = sensors.list_sensors(session, env["task"].id)[0]
    assert row.is_free
    assert recycle.list_recycle_items(session), "被摘掉的节点应进回收站"


def test_rebind_assembled_sensor_to_unassembled_object_is_rejected(session, env):
    sensors.bind_object(session, env["sensor"].id, env["motor"].id)
    root = tree.root_node(session, env["task"].id)
    device = tree.create_node(session, env["task"].id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        env["task"].id,
        "component",
        "入片牵引电机",
        parent_id=device.id,
        ref_type="monitor_object",
        ref_id=env["motor"].id,
    )
    ch = channel_reg.list_channels(session, env["task"].id)[0]
    node = tree.create_node(
        session,
        env["task"].id,
        "channel",
        ch.code,
        parent_id=component.id,
        ref_type="channel_reg",
        ref_id=ch.id,
    )
    sensors.attach_node(session, env["sensor"].id, node.id)

    with pytest.raises(sensors.SensorError, match="装配"):
        sensors.bind_object(session, env["sensor"].id, env["bearing"].id)
