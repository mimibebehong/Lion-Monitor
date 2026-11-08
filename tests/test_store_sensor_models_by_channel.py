"""通道下拉框的传感器型号必须按「挂在哪个通道节点」解析，不能靠文本通道号。

2026-11-02：「为什么 CH1 显示未挂传感器」——传感器 #1 的 channel_code 是空的，
只挂在通道节点上，于是按 channel_code 建映射时漏掉了它。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    object_registry,
    sensors,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import Sensor  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    path = tmp_path / "models.db"
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="型号解析")
        obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(
            session,
            task.id,
            "component",
            obj.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj.id,
        )
        ch1 = channel_reg.create_channel(session, task.id, obj.id, "CH1")
        node = tree.create_node(
            session,
            task.id,
            "channel",
            ch1.code,
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=ch1.id,
        )
        # 老数据：channel_code 为空，只靠 node_id 关联
        created = sensors.create_sensor(
            session, task.id, node.id, model="IEPE 500mV/g", sensitivity="500 mV/g"
        )
        session.get(Sensor, created.id).channel_code = ""
        session.flush()
        ch2 = channel_reg.create_channel(session, task.id, None, "CH2")
        ids = {"ch1": ch1.id, "ch2": ch2.id, "node": node.id, "sensor": created.id}
    yield {"factory": factory, **ids}


def test_channel_without_code_still_reports_its_sensor(env):
    with env["factory"]() as session:
        mapping = sensors.models_by_channel(session, 1)
    assert mapping.get(env["ch1"]) == "IEPE 500mV/g"


def test_channel_without_sensor_is_absent(env):
    with env["factory"]() as session:
        mapping = sensors.models_by_channel(session, 1)
    assert env["ch2"] not in mapping


def test_falls_back_to_channel_code(env):
    """没有 node_id 但写了通道号的老数据也能解析"""
    with env["factory"]() as session:
        row = Sensor(task_id=1, channel_code="CH2", model="IEPE 100mV/g", status="启用")
        session.add(row)
        session.flush()
        mapping = sensors.models_by_channel(session, 1)
    assert mapping.get(env["ch2"]) == "IEPE 100mV/g"
