"""传感器页：归属列 + 就地改绑/解绑（马哥 2026-11-02 口径）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "pages" / "sensors.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    object_registry,
    sensors,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "sen.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="归属任务")
            self.motor = object_registry.create_object(
                session, self.task.id, "入片牵引电机", "电机"
            )
            self.bearing = object_registry.create_object(
                session, self.task.id, "隔膜过辊轴承", "轴承"
            )
            channel_reg.create_channel(session, self.task.id, None, "CH2")
            self.sensor = sensors.create_sensor(
                session, self.task.id, node_id=None, model="IEPE 500mV/g", channel_code="CH2"
            )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env, picked: int | None = 0):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=120).run()
    if picked is not None:
        at.session_state["sensor_table"] = {"selection": {"rows": [picked], "columns": []}}
        at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def test_table_shows_free_for_unbound_sensor(env):
    at = _app(env, picked=None)

    frame = at.dataframe[0].value
    assert "归属" in frame.columns
    assert frame.iloc[0]["归属"] == "自由"


def test_bind_free_sensor_to_object_from_detail(env):
    at = _app(env)
    box = [b for b in at.selectbox if b.label == "归属监测对象"][0]
    assert list(box.options)[0].startswith("自由")
    box.set_value("隔膜过辊轴承（轴承）")
    [b for b in at.button if b.label == "保存归属"][0].click()
    at.run()
    assert not at.exception

    with env.session() as session:
        row = sensors.list_sensors(session, env.task.id)[0]
    assert (row.object_id, row.object_name, row.is_free) == (
        env.bearing.id,
        "隔膜过辊轴承",
        False,
    )


def test_unbind_from_detail_takes_node_out_of_tree(env):
    # 先绑定并装配到健康树
    with env.session() as session:
        sensors.bind_object(session, env.sensor.id, env.motor.id)
        root = tree.root_node(session, env.task.id)
        device = tree.create_node(session, env.task.id, "device", "卷绕产线", parent_id=root.id)
        component = tree.create_node(
            session,
            env.task.id,
            "component",
            "入片牵引电机",
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=env.motor.id,
        )
        ch = next(c for c in channel_reg.list_channels(session, env.task.id) if c.code == "CH2")
        node = tree.create_node(
            session,
            env.task.id,
            "channel",
            ch.code,
            parent_id=component.id,
            ref_type="channel_reg",
            ref_id=ch.id,
        )
        sensors.attach_node(session, env.sensor.id, node.id)
        node_id = node.id

    at = _app(env)
    box = [b for b in at.selectbox if b.label == "归属监测对象"][0]
    box.set_value("自由（不归属任何监测对象）")
    [b for b in at.button if b.label == "保存归属"][0].click()
    at.run()
    assert not at.exception

    from lion_monitor.store.models import TaskNode

    with env.session() as session:
        assert session.get(TaskNode, node_id).deleted_at is not None
        row = sensors.list_sensors(session, env.task.id)[0]
    assert row.is_free
