"""CF-2 验收 5：健康树节点属性里的「特征频率」覆盖（AppTest 真跑页面）。

口径：节点手填 > 对象派生；界面每行标「来源：节点手填 / 对象派生 / 未配置」。
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
    component_freq,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402

ATTACHMENT = ROOT / "tests" / "data" / "examples" / "监测对象_20260309.csv"


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "ht.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="频率覆盖任务")
            self.obj = object_registry.create_object(
                session, self.task.id, name="主减速器", obj_type="轴承"
            )
            object_registry.set_rated_rpm(session, self.obj.id, 21000.0)
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(session, self.task.id, "device", "主减", parent_id=root.id)
            self.component = tree.create_node(
                session,
                self.task.id,
                "component",
                "主减速器",
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=self.obj.id,
            )
            self.channel = tree.create_node(
                session, self.task.id, "channel", "Chan_1", parent_id=self.component.id
            )
            component_freq.import_csv(session, self.obj.id, ATTACHMENT)

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def node(self, node_id):
        with self.session() as session:
            return tree.get_node(session, node_id)

    def resolve(self, node_id, kind="om1", part_code="Z1_R"):
        with self.session() as session:
            return component_freq.resolve_component_freq(
                session, node_id, kind, part_code=part_code
            )


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


def _button(at, label: str):
    for element in at.button:
        if element.label == label:
            return element
    raise AssertionError(f"找不到按钮 {label}：{[b.label for b in at.button]}")


def _captions(at) -> list[str]:
    return [element.value for element in at.caption]


def test_channel_node_shows_ledger_sources(env):
    at = _app(env, env.channel.id)

    captions = _captions(at)
    assert any("📐 特征频率" in str(item.value) for item in at.markdown), [
        item.value for item in at.markdown
    ]
    assert any("转速来源" in text and "21000" in text for text in captions), captions
    frame = at.dataframe[0].value
    assert {"部件", "频率项", "对象派生 (Hz)", "节点手填 (Hz)", "来源"} <= set(frame.columns)
    z1 = frame[frame["部件"] == "Z1_R"].iloc[0]
    assert z1["来源"] == "对象派生"
    assert z1["对象派生 (Hz)"] == pytest.approx(350.0)  # 348.3/20898×21000


def test_set_override_writes_node_attrs_and_wins(env):
    at = _app(env, env.channel.id)

    at.selectbox(key=f"fo_pick_{env.channel.id}").set_value("Z1_R · OM1（1×转频）")
    at.number_input(key=f"fo_value_{env.channel.id}").set_value(400.0)
    _button(at, "保存覆盖").click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    attrs = env.node(env.channel.id).attrs
    assert attrs[component_freq.freq_override_key("Z1_R", "om1")] == "400"
    resolved = env.resolve(env.channel.id)
    assert resolved is not None
    assert resolved.value_hz == pytest.approx(400.0)
    assert resolved.source == "node"
    frame = at.dataframe[0].value
    z1 = frame[frame["部件"] == "Z1_R"].iloc[0]
    assert z1["来源"] == "节点手填"
    assert z1["节点手填 (Hz)"] == pytest.approx(400.0)


def test_clear_override_returns_to_object_derivation(env):
    at = _app(env, env.channel.id)
    at.selectbox(key=f"fo_pick_{env.channel.id}").set_value("Z1_R · OM1（1×转频）")
    at.number_input(key=f"fo_value_{env.channel.id}").set_value(400.0)
    _button(at, "保存覆盖").click()
    at.run()

    at.selectbox(key=f"fo_pick_{env.channel.id}").set_value("Z1_R · OM1（1×转频）")
    _button(at, "清除覆盖").click()
    at.run()

    assert component_freq.freq_override_key("Z1_R", "om1") not in env.node(env.channel.id).attrs
    resolved = env.resolve(env.channel.id)
    assert resolved is not None
    assert resolved.source == "object"
    assert resolved.value_hz == pytest.approx(350.0)


def test_override_on_component_node_inherited_by_channel(env):
    at = _app(env, env.component.id)

    at.selectbox(key=f"fo_pick_{env.component.id}").set_value("Z1_R · OM2（2×转频）")
    at.number_input(key=f"fo_value_{env.component.id}").set_value(700.0)
    _button(at, "保存覆盖").click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    resolved = env.resolve(env.channel.id, kind="om2")
    assert resolved is not None
    assert resolved.value_hz == pytest.approx(700.0)
    assert resolved.source == "node"


def test_node_without_object_shows_hint(env):
    with env.session() as session:
        root = tree.root_node(session, env.task.id)
        lonely = tree.create_node(session, env.task.id, "device", "孤立设备", parent_id=root.id)
        comp = tree.create_node(
            session, env.task.id, "component", "无台账对象", parent_id=lonely.id
        )
        loose = tree.create_node(
            session,
            env.task.id,
            "channel",
            "CH_L",
            parent_id=comp.id,
        )
    at = _app(env, loose.id)
    assert any("未关联监测对象" in text for text in _captions(at)), _captions(at)
