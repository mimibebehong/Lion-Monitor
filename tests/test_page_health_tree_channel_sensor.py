"""健康树：通道属性与传感器页一致（单一来源），新增通道只列本对象的通道。

2026-11-02 第 2、3 条整改。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "health_tree.py"
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
        path = tmp_path / "ht.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="整改任务")
            motor = object_registry.create_object(session, self.task.id, "入片牵引电机", "电机")
            bearing = object_registry.create_object(session, self.task.id, "隔膜过辊轴承", "轴承")
            ch3 = channel_reg.create_channel(session, self.task.id, motor.id, "CH3")
            channel_reg.create_channel(session, self.task.id, motor.id, "CH4")  # 自由通道
            # 别的对象名下的通道
            channel_reg.create_channel(session, self.task.id, bearing.id, "CH2")
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            self.component = tree.create_node(
                session,
                self.task.id,
                "component",
                motor.name,
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=motor.id,
            )
            self.channel = tree.create_node(
                session,
                self.task.id,
                "channel",
                ch3.code,
                parent_id=self.component.id,
                ref_type="channel_reg",
                ref_id=ch3.id,
            )
            self.sensor = sensors.create_sensor(
                session,
                self.task.id,
                self.channel.id,
                model="IEPE 500mV/g",
                sensitivity="500 mV/g",
                range_g="50 g",
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


def _app(env, node_id):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = node_id
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def test_channel_attr_is_read_only_and_matches_sensor_ledger(env):
    at = _app(env, env.channel.id)

    assert not [b for b in at.button if b.label == "保存通道属性"], "通道属性不该再有本地保存按钮"
    texts = " ".join(str(item.value) for item in at.markdown)
    assert "IEPE 500mV/g" in texts
    assert "500 mV/g" in texts
    captions = " ".join(str(item.value) for item in at.caption)
    assert "传感器" in captions and "页维护" in captions


def test_channel_attr_follows_sensor_page_edit(env):
    with env.session() as session:
        sensors.update_sensor(session, env.sensor.id, model="IEPE 100mV/g")

    at = _app(env, env.channel.id)

    texts = " ".join(str(item.value) for item in at.markdown)
    assert "IEPE 100mV/g" in texts
    assert "IEPE 500mV/g" not in texts


def test_new_channel_pool_lists_all_channels_of_the_task(env):
    """一台传感器可挂多个监测对象：下拉框列出全台账通道（含别的对象名下的）"""
    at = _app(env, env.component.id)

    boxes = [b for b in at.selectbox if "选择传感器通道" in b.label]
    assert boxes, [b.label for b in at.selectbox]
    options = list(boxes[0].options)
    # CH3 已装在本节点下 → 排除；CH4 属本对象、CH2 属别的对象 → 都列出来
    codes = {item.split(" · ")[0] for item in options if item != "＋ 新建通道…"}
    assert codes == {"CH2", "CH4"}, options
    assert any("自由" in item or "·" in item for item in options), options
    assert "＋ 新建通道…" in options, options


def test_new_channel_button_always_available_for_new_channel(env):
    """台账里没有空闲通道时，仍可用下拉框最后一项就地新建"""
    at = _app(env, env.component.id)
    picker = next(b for b in at.selectbox if "选择传感器通道" in b.label)
    assert "＋ 新建通道…" in picker.options
