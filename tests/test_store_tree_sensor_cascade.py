"""删掉通道节点 = 解绑：传感器留在台账当自由态，恢复节点时自动挂回去。

马哥 2026-11-02：「我都把下面的三个监测对象监测的传感器节点删除了，为什么传感器还没有
恢复自由状态？」——装配是树上的关系，删装配不能把传感器一起删掉。
"""

from __future__ import annotations

import json

import pytest

from lion_monitor.store import channel_reg, object_registry, recycle, sensors, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store.models import DeletionLog, Sensor


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.session_scope(store_db.make_session_factory(engine)) as s:
        yield s


@pytest.fixture()
def env(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    obj = object_registry.create_object(session, task.id, "入片牵引电机", "电机")
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH3")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        task.id,
        "component",
        obj.name,
        parent_id=device.id,
        ref_type="monitor_object",
        ref_id=obj.id,
    )
    channel = tree.create_node(
        session,
        task.id,
        "channel",
        ch.code,
        parent_id=component.id,
        ref_type="channel_reg",
        ref_id=ch.id,
    )
    sensor = sensors.create_sensor(
        session, task.id, channel.id, model="IEPE 500mV/g", sensitivity="500 mV/g"
    )
    return {"task": task, "obj": obj, "channel_node": channel, "sensor": sensor}


def test_deleting_channel_node_frees_its_sensor(session, env):
    counts = tree.soft_delete_node(session, env["channel_node"].id)

    assert counts.get("channel") == 1
    rows = sensors.list_sensors(session, env["task"].id)
    assert [r.id for r in rows] == [env["sensor"].id], "传感器必须留在台账"
    assert rows[0].node_id is None, "删装配后应回到自由态"
    row = session.get(Sensor, env["sensor"].id)
    assert row.deleted_at is None


def test_deletion_payload_records_the_sensor(session, env):
    tree.soft_delete_node(session, env["channel_node"].id)

    log = session.execute(
        DeletionLog.__table__.select().where(DeletionLog.mode == "soft").limit(1)
    ).first()
    payload = json.loads(log.payload)
    assert [item["id"] for item in payload["sensors"]] == [env["sensor"].id]
    assert payload["sensors"][0]["model"] == "IEPE 500mV/g"


def test_restore_brings_node_and_sensor_back(session, env):
    tree.soft_delete_node(session, env["channel_node"].id)
    log_id = recycle.list_recycle_items(session)[0].log_id

    result = recycle.restore_item(session, log_id)

    assert result.ok, result.message
    assert tree.get_node(session, env["channel_node"].id).deleted_at is None
    rows = sensors.list_sensors(session, env["task"].id)
    assert [(r.id, r.model) for r in rows] == [(env["sensor"].id, "IEPE 500mV/g")]


def test_deleting_parent_object_node_frees_nested_channel_sensors(session, env):
    component_id = tree.get_node(session, env["channel_node"].id).parent_id

    tree.soft_delete_node(session, component_id)

    rows = sensors.list_sensors(session, env["task"].id)
    assert [r.id for r in rows] == [env["sensor"].id]
    assert rows[0].node_id is None


def test_channel_ownership_follows_assembly(session, env):
    """删掉装配后，通道台账的归属要回到自由（马哥的原始诉求）"""
    tree.soft_delete_node(session, env["channel_node"].id)

    rows = channel_reg.list_channels(session, env["task"].id)
    assert [row.is_free for row in rows if row.code == "CH3"] == [True], rows


def test_channel_node_without_sensor_deletes_cleanly(session, env):
    obj2 = object_registry.create_object(session, env["task"].id, "卷绕张力气缸", "气缸")
    ch2 = channel_reg.create_channel(session, env["task"].id, obj2.id, "CH9")
    bare = tree.create_node(
        session,
        env["task"].id,
        "channel",
        ch2.code,
        parent_id=tree.get_node(session, env["channel_node"].id).parent_id,
        ref_type="channel_reg",
        ref_id=ch2.id,
    )

    tree.soft_delete_node(session, bare.id)

    # 另一个通道的传感器不受影响
    assert [r.id for r in sensors.list_sensors(session, env["task"].id)] == [env["sensor"].id]
