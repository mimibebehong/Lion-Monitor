"""通道属性要跟传感器页一致，新增通道只列本对象的通道。

2026-11-02：
- 2）健康树「通道属性（传感器信息在「传感器」页维护）」的内容应与传感器页保持一致
  → 只能有一个来源：sensor 台账。健康树只读展示，不再往节点 attrs 里存第二份。
- 3）自由状态的通道不该顶着别的监测对象的名字出现在下拉里
  → 新增通道的可选项按「当前对象」过滤，标签只写通道号。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import channel_reg, object_registry, sensors, tasks, tree
from lion_monitor.store import db as store_db


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.session_scope(store_db.make_session_factory(engine)) as s:
        yield s


@pytest.fixture()
def env(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    motor = object_registry.create_object(session, task.id, "入片牵引电机", "电机")
    bearing = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承")
    ch_motor = channel_reg.create_channel(session, task.id, motor.id, "CH3")
    ch_bearing = channel_reg.create_channel(session, task.id, bearing.id, "CH2")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        task.id,
        "component",
        motor.name,
        parent_id=device.id,
        ref_type="monitor_object",
        ref_id=motor.id,
    )
    node = tree.create_node(
        session,
        task.id,
        "channel",
        ch_motor.code,
        parent_id=component.id,
        ref_type="channel_reg",
        ref_id=ch_motor.id,
    )
    return {
        "task": task,
        "motor": motor,
        "bearing": bearing,
        "ch_motor": ch_motor,
        "ch_bearing": ch_bearing,
        "node": node,
    }


def test_available_channels_are_scoped_to_the_object(session, env):
    rows = channel_reg.list_channels(session, env["task"].id)

    scoped = channel_reg.available_for_object(rows, env["motor"].id, used=set())

    assert [c.code for c in scoped] == ["CH3"]


def test_available_channels_skip_already_assembled_codes(session, env):
    rows = channel_reg.list_channels(session, env["task"].id)

    assert channel_reg.available_for_object(rows, env["motor"].id, used={"CH3"}) == []


def test_available_channels_without_object_returns_everything(session, env):
    rows = channel_reg.list_channels(session, env["task"].id)

    assert {c.code for c in channel_reg.available_for_object(rows, None, used=set())} == {
        "CH3",
        "CH2",
    }


def test_sensor_of_node_is_the_single_source(session, env):
    created = sensors.create_sensor(
        session, env["task"].id, env["node"].id, model="IEPE 500mV/g", sensitivity="500 mV/g"
    )

    row = sensors.sensor_of_node(session, env["node"].id)
    assert (row.id, row.model, row.sensitivity) == (created.id, "IEPE 500mV/g", "500 mV/g")

    # 在传感器页改了，健康树读到的是同一个值（不存在第二份）
    sensors.update_sensor(session, created.id, model="IEPE 100mV/g")
    assert sensors.sensor_of_node(session, env["node"].id).model == "IEPE 100mV/g"


def test_sensor_of_node_is_none_when_unbound_or_missing(session, env):
    assert sensors.sensor_of_node(session, env["node"].id) is None
    assert sensors.sensor_of_node(session, 999_999) is None


def test_sensor_of_node_ignores_deleted_sensor(session, env):
    created = sensors.create_sensor(session, env["task"].id, env["node"].id, model="IEPE 500mV/g")

    sensors.delete_sensor(session, created.id)

    assert sensors.sensor_of_node(session, env["node"].id) is None
