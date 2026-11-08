"""健康树：额定转速属于「监测对象」节点；通道节点只读显示生效转速（马哥 2026-11-02）。"""

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
        path = tmp_path / "rated_rpm.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="转速归属")
            bearing = object_registry.create_object(session, self.task.id, "隔膜过辊轴承", "轴承")
            ch1 = channel_reg.create_channel(session, self.task.id, bearing.id, "CH1")
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            self.component = tree.create_node(
                session,
                self.task.id,
                "component",
                bearing.name,
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=bearing.id,
            )
            self.channel = tree.create_node(
                session,
                self.task.id,
                "channel",
                ch1.code,
                parent_id=self.component.id,
                ref_type="channel_reg",
                ref_id=ch1.id,
            )
            self.object_id = bearing.id

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def rated_rpm(self) -> float | None:
        with self.session() as session:
            rows = {row.id: row for row in object_registry.list_objects(session, self.task.id)}
        return rows[self.object_id].rated_rpm


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(node_id):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = node_id
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _rpm_input(at):
    found = [item for item in at.number_input if "额定转速" in item.label]
    assert found, [item.label for item in at.number_input]
    return found[0]


def test_component_node_offers_rated_rpm_input(env):
    at = _app(env.component.id)

    assert _rpm_input(at) is not None
    assert [b for b in at.button if b.label == "保存额定转速"], [b.label for b in at.button]


def test_saving_rated_rpm_writes_object_ledger(env):
    at = _app(env.component.id)
    _rpm_input(at).set_value(1500.0).run()
    assert [exc.value for exc in at.exception] == []

    save = [b for b in at.button if b.label == "保存额定转速"][0]
    save.click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert env.rated_rpm() == pytest.approx(1500.0)


def test_channel_node_has_no_manual_rpm_input(env):
    at = _app(env.channel.id)

    assert not [i for i in at.number_input if "转速" in i.label], [i.label for i in at.number_input]
    assert not [b for b in at.button if b.label == "保存转速"], [b.label for b in at.button]


def test_channel_shows_effective_rpm_from_object_rating(env):
    with env.session() as session:
        object_registry.set_rated_rpm(session, env.object_id, 1450.0)

    at = _app(env.channel.id)

    captions = " ".join(str(item.value) for item in at.caption)
    assert "1450" in captions
    assert "对象额定" in captions
