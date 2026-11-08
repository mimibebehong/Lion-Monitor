"""健康树上就地新建对象 / 通道 / 传感器（装配不跳页）。"""

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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "quick_create.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="就地新建")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        obj = object_registry.create_object(session, task.id, "辊压机", "轴承", part="驱动端")
        comp = tree.create_node(
            session,
            task.id,
            "component",
            obj.name,
            parent_id=device.id,
            attrs={"类型": obj.obj_type},
            ref_type="monitor_object",
            ref_id=obj.id,
        )
        row = channel_reg.create_channel(session, task.id, obj.id, "CH1")
        channel = tree.create_node(
            session,
            task.id,
            "channel",
            row.code,
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=row.id,
        )
        ids = {
            "task": task.id,
            "device": device.id,
            "component": comp.id,
            "object": obj.id,
            "channel": channel.id,
        }
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, **ids}
    store_ctx._session_factory.clear()


def _app(env, node_id):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = node_id
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _text(at, label):
    for item in at.text_input:
        if item.label == label:
            return item
    raise AssertionError(f"没找到输入框 {label}；现有 {[t.label for t in at.text_input]}")


def _number(at, label):
    for item in at.number_input:
        if item.label == label:
            return item
    raise AssertionError(f"没找到数字框 {label}；现有 {[n.label for n in at.number_input]}")


def _button(at, label):
    for item in at.button:
        if item.label == label:
            return item
    raise AssertionError(f"没找到按钮 {label}；现有 {[b.label for b in at.button]}")


def test_quick_create_component_with_rated_rpm(env):
    at = _app(env, env["device"])
    _text(at, "对象名称").set_value("入片牵引电机")
    _number(at, "额定转速（rpm）").set_value(1480.0)
    _button(at, "新建并装配").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    with env["factory"]() as session:
        objs = {row.name: row for row in object_registry.list_objects(session, env["task"])}
        nodes = [
            node for node in tree.list_nodes(session, env["task"]) if node.node_type == "component"
        ]
    created = objs["入片牵引电机"]
    assert created.rated_rpm == pytest.approx(1480.0)
    assert created.part == ""
    node = next(item for item in nodes if item.name == "入片牵引电机")
    assert (node.ref_type, node.ref_id) == ("monitor_object", created.id)
    assert node.parent_id == env["device"]


def test_new_channel_option_inside_same_dropdown(env):
    """新建通道不再单开一个入口：下拉框最后一项「＋ 新建通道…」里输入通道号。"""
    with env["factory"]() as session:
        channel_reg.create_channel(session, env["task"], None, "CH2")  # 自由通道，给下拉框一项
    at = _app(env, env["component"])
    picker = next(item for item in at.selectbox if "选择传感器通道" in item.label)
    assert "＋ 新建通道…" in picker.options
    picker.set_value("＋ 新建通道…").run()
    _text(at, "新通道号").set_value("CH9")
    _button(at, "装配通道").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    with env["factory"]() as session:
        rows = {row.code: row for row in channel_reg.list_channels(session, env["task"])}
        nodes = [
            node for node in tree.list_nodes(session, env["task"]) if node.node_type == "channel"
        ]
    created = rows["CH9"]
    assert created.object_id == env["object"]
    node = next(item for item in nodes if item.name == "CH9")
    assert (node.ref_type, node.ref_id) == ("channel_reg", created.id)
    assert node.parent_id == env["component"]


def test_quick_create_sensor_on_channel(env):
    at = _app(env, env["channel"])
    _text(at, "型号").set_value("IEPE 100mV/g")
    _text(at, "灵敏度").set_value("100 mV/g")
    _text(at, "量程（g）").set_value("±50")
    _text(at, "安装方式").set_value("螺纹")
    _text(at, "标定日期").set_value("2026-10-01")
    _button(at, "录入并挂上").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    with env["factory"]() as session:
        rows = sensors.list_sensors(session, env["task"])
    assert len(rows) == 1
    assert rows[0].node_id == env["channel"]
    assert rows[0].model == "IEPE 100mV/g"
    assert rows[0].sensitivity == "100 mV/g"
    assert rows[0].status == "启用"


def test_dropdown_lists_channels_of_other_objects_without_owner(env):
    """一台传感器可挂多个对象：下拉框列出别对象的通道，但标签不写归属对象"""
    with env["factory"]() as session:
        other = object_registry.create_object(session, env["task"], "另一台电机", "电机")
        channel_reg.create_channel(session, env["task"], other.id, "CH7")
    at = _app(env, env["component"])
    picker = next(item for item in at.selectbox if "选择传感器通道" in item.label)
    options = [option for option in picker.options if option != "＋ 新建通道…"]
    assert any(option.startswith("CH7") for option in options), options
    assert not any("电机" in option for option in options), options
    assert all(len(option.split(" · ")) == 2 for option in options), options


def test_expander_keeps_open_and_restores_position_after_assembly(env):
    """装配完成后页面重跑：展开条保持展开，并留下回到原位的锚点标记"""
    with env["factory"]() as session:
        channel_reg.create_channel(session, env["task"], None, "CH2")
    at = _app(env, env["component"])
    picker = next(item for item in at.selectbox if "选择传感器通道" in item.label)
    picker.set_value(next(o for o in picker.options if o.startswith("CH2"))).run()
    _button(at, "装配通道").click().run()

    assert at.session_state[f"ht_addopen_{env['component']}"] is True
    assert [exc.value for exc in at.exception] == []
    # 重跑后展开条仍然在，且没有被收起（expanded 由 session_state 决定）
    labels = [item.label for item in at.expander]
    assert any("新增通道" in label for label in labels), labels
