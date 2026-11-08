"""传感器子模块（store.sensors）：通道上的传感器台账。"""

from __future__ import annotations

import pytest

from lion_monitor.store import sensors, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def channel(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    return tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)


def test_create_sensor_attaches_to_channel(session, channel):
    row = sensors.create_sensor(
        session,
        channel.task_id,
        channel.id,
        model="IEPE 500mV/g",
        sensitivity="500 mV/g",
        range_g="±10 g",
        mounting="磁吸",
        calibrated_on="2026-09-20",
    )

    assert row.channel_name == "CH1"
    assert row.sensitivity == "500 mV/g"
    assert row.mounting == "磁吸"
    assert row.status == "启用"


def test_create_sensor_rejects_non_channel(session, channel):
    with pytest.raises(sensors.SensorError, match="通道"):
        sensors.create_sensor(session, channel.task_id, channel.parent_id, model="X")


def test_create_sensor_on_configured_channel_retires_previous(session, channel):
    old = sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    new = sensors.create_sensor(session, channel.task_id, channel.id, model="B")

    rows = {row.model: row.status for row in sensors.list_sensors(session, channel.task_id)}
    assert rows == {"A": "停用", "B": "启用"}
    assert sensors.get_sensor(session, old.id).status == "停用"
    assert new.status == "启用"


def test_create_sensor_requires_model(session, channel):
    with pytest.raises(sensors.SensorError, match="型号"):
        sensors.create_sensor(session, channel.task_id, channel.id, model="  ")


def test_list_sensors_by_task(session, channel):
    sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    rows = sensors.list_sensors(session, channel.task_id)

    assert [row.model for row in rows] == ["A"]
    assert rows[0].channel_name == "CH1"


def test_update_sensor_fields(session, channel):
    row = sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    updated = sensors.update_sensor(session, row.id, model="B", status="停用")

    assert updated.model == "B"
    assert updated.status == "停用"


def test_update_sensor_rejects_bad_status(session, channel):
    row = sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    with pytest.raises(sensors.SensorError, match="状态"):
        sensors.update_sensor(session, row.id, status="报废")


def test_delete_sensor_hides_from_list(session, channel):
    row = sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    sensors.delete_sensor(session, row.id)

    assert sensors.list_sensors(session, channel.task_id) == []


def test_create_sensor_without_channel(session, channel):
    row = sensors.create_sensor(session, channel.task_id, None, model="库存传感器-1")

    assert row.node_id is None
    assert row.channel_name == "未分配"
    assert [r.model for r in sensors.list_sensors(session, channel.task_id)] == ["库存传感器-1"]


def test_assign_unassigned_sensor_to_channel(session, channel):
    row = sensors.create_sensor(session, channel.task_id, None, model="库存传感器-1")

    updated = sensors.update_sensor(session, row.id, node_id=channel.id)

    assert updated.node_id == channel.id
    assert updated.channel_name == "CH1"


def test_assign_rejects_non_channel(session, channel):
    row = sensors.create_sensor(session, channel.task_id, None, model="库存传感器-1")

    with pytest.raises(sensors.SensorError, match="通道"):
        sensors.update_sensor(session, row.id, node_id=channel.parent_id)


def test_unassigned_sorted_after_assigned(session, channel):
    sensors.create_sensor(session, channel.task_id, None, model="库存")
    sensors.create_sensor(session, channel.task_id, channel.id, model="CH1 在用")

    assert [r.model for r in sensors.list_sensors(session, channel.task_id)] == ["CH1 在用", "库存"]


def test_create_sensor_with_manual_channel_code(session, channel):
    row = sensors.create_sensor(
        session, channel.task_id, None, model="手持传感器", channel_code="CH9"
    )

    assert row.channel_code == "CH9"
    assert row.node_id is None
    assert row.channel_name == "CH9"


def test_manual_code_matching_existing_channel_links_node(session, channel):
    row = sensors.create_sensor(session, channel.task_id, None, model="A", channel_code="CH1")

    assert row.node_id == channel.id
    assert row.channel_name == "CH1"


def test_update_sensor_channel_code(session, channel):
    row = sensors.create_sensor(session, channel.task_id, channel.id, model="A")

    updated = sensors.update_sensor(session, row.id, channel_code="CH7")

    assert updated.channel_code == "CH7"
    assert updated.channel_name == "CH7"
