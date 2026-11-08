"""健康树删节点：统一改为二次点击确认（2026-11-03）。

老写法是「确认删除该节点及其子树」勾选框 + 禁用按钮；现在与监测对象 / 传感器 / 阈值页一致：
第一次点「删除节点」只进待确认（变红「确认删除该节点及子树？」），第二次才连子树一起删。
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
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "ht_del.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="健康树删除任务")
            root = tree.root_node(session, self.task.id)
            self.device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            self.bearing = object_registry.create_object(
                session, self.task.id, "隔膜过辊轴承", "轴承"
            )
            self.roller = object_registry.create_object(
                session, self.task.id, "收卷辊", "辊", part="收卷"
            )
            self.comp_a = tree.create_node(
                session,
                self.task.id,
                "component",
                "隔膜过辊轴承",
                parent_id=self.device.id,
                ref_type="monitor_object",
                ref_id=self.bearing.id,
            )
            self.comp_b = tree.create_node(
                session,
                self.task.id,
                "component",
                "收卷辊",
                parent_id=self.device.id,
                ref_type="monitor_object",
                ref_id=self.roller.id,
            )
            ch = channel_reg.create_channel(session, self.task.id, self.bearing.id, "CH1")
            tree.create_node(
                session,
                self.task.id,
                "channel",
                ch.code,
                parent_id=self.comp_a.id,
                ref_type="channel_reg",
                ref_id=ch.id,
            )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def names(self) -> list[str]:
        with self.session() as session:
            return [row.name for row in tree.list_nodes(session, self.task.id)]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(node_id: int):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = node_id
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _labels(at):
    return [button.label for button in at.button]


def _html(at):
    return "\n".join(str(block.value) for block in at.markdown)


def test_delete_node_has_no_confirm_checkbox(env):
    at = _app(env.comp_a.id)

    boxes = [box.label for box in at.checkbox]
    assert not [label for label in boxes if "确认删除该节点及其子树" in label], boxes
    assert _labels(at).count("删除节点") == 1


def test_delete_node_needs_two_clicks_and_takes_subtree(env):
    at = _app(env.comp_a.id)
    assert "CH1" in env.names()

    at.button(key=f"del_{env.comp_a.id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除该节点及子树？" in _labels(at), _labels(at)
    assert "隔膜过辊轴承" in env.names(), "第一次点击不该真的删除"
    assert "CH1" in env.names(), "第一次点击不该动子树"
    html = _html(at)
    assert f"del_{env.comp_a.id}" in html
    assert "d93a3a" in html.lower()

    at.button(key=f"del_{env.comp_a.id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    names = env.names()
    assert "隔膜过辊轴承" not in names
    assert "CH1" not in names, "子树要一起删"


def test_tab_switch_wiring_disarms_delete():
    """切页签要撤掉删除待确认态。

    Streamlit 的 tabs 选中态只有真实浏览器才会触发 on_change（AppTest 里给 tab key 赋值
    只记录 widget state、不跑回调），所以这里做源码级守卫：接线必须还在。
    """
    src = APP_PAGE.read_text(encoding="utf-8")
    assert "on_change=_reset_node_delete_arm" in src
    assert "args=(node.id,)" in src
    assert 'st.session_state.pop(f"del_node_armed_{node_id}", None)' in src


def test_delete_node_arming_resets_when_selection_switches(env):
    at = _app(env.comp_a.id)

    at.button(key=f"del_{env.comp_a.id}").click().run()
    assert "确认删除该节点及子树？" in _labels(at)

    at.session_state["ht_selected"] = env.comp_b.id
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除该节点及子树？" not in _labels(at), _labels(at)

    at.button(key=f"del_{env.comp_b.id}").click().run()
    assert "收卷辊" in env.names(), "切到另一节点后第一次点击不该删除"

    at.button(key=f"del_{env.comp_b.id}").click().run()
    names = env.names()
    assert "收卷辊" not in names
    assert "隔膜过辊轴承" in names, "不该连带删掉之前那个节点"
