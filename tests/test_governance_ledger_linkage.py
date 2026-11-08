"""治理项：参数卡唯一索引 + 台账装配标记 + 悬空引用清理。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_OBJECTS = ROOT / "app" / "pages" / "objects.py"
APP_TREE = ROOT / "app" / "pages" / "health_tree.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "governance.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="治理")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        obj = object_registry.create_object(session, task.id, "辊压机", "轴承")
        free_obj = object_registry.create_object(session, task.id, "备用泵", "泵")
        comp = tree.create_node(
            session,
            task.id,
            "component",
            obj.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj.id,
        )
        chan = channel_reg.create_channel(session, task.id, obj.id, "CH1")
        channel = tree.create_node(
            session,
            task.id,
            "channel",
            chan.code,
            parent_id=comp.id,
            ref_type="channel_reg",
            ref_id=chan.id,
        )
        metric = tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=channel.id,
            attrs={"code": "v_RMS"},
        )
        metric_def.seed_defaults(session, task.id)
        metric_assign.assign_metric(session, task.id, metric.id, "v_RMS")
        loose = tree.create_node(
            session,
            task.id,
            "component",
            "老节点",
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=free_obj.id,
        )
        ids = {
            "task": task.id,
            "device": device.id,
            "object": obj.id,
            "free_object": free_obj.id,
            "component": comp.id,
            "channel": channel.id,
            "metric": metric.id,
            "loose": loose.id,
            "db_path": str(path),
        }
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, **ids}
    store_ctx._session_factory.clear()


def test_metric_assign_unique_index_blocks_duplicates(env):
    clone = (
        "insert into metric_assign"
        " (task_id, node_id, metric_code, unit, params_json, params_locked, enabled)"
        " select task_id, node_id, metric_code, unit, params_json, params_locked, enabled"
        " from metric_assign where id=?"
    )
    with env["factory"]() as session:
        first = metric_assign.list_assignments(session, env["task"])[0]
    conn = sqlite3.connect(env["db_path"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(clone, (first.id,))
        conn.rollback()
        # 软删后可以再建：部分索引只看活着的行
        conn.execute(
            "update metric_assign set deleted_at='2026-01-01 00:00:00' where id=?", (first.id,)
        )
        conn.execute(clone, (first.id,))
        conn.commit()
        assert conn.execute("select count(*) from metric_assign").fetchone()[0] == 2
    finally:
        conn.close()


def test_objects_page_marks_assembled(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_OBJECTS), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    frame = at.dataframe[0].value
    marks = dict(zip(frame["名称"], frame["装配"], strict=True))
    assert marks["辊压机"] == "已装配"
    assert marks["备用泵"] == "已装配"  # 老节点也引用着它
    captions = " ".join(str(item.value) for item in at.caption)
    assert "未装配" in captions or "自由" in captions


def test_dangling_ref_can_be_cleared(env):
    with env["factory"]() as session:
        object_registry.delete_object(session, env["free_object"])

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_TREE), default_timeout=180).run()
    at.session_state["ht_selected"] = env["loose"]
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    warnings = " ".join(str(item.value) for item in at.warning)
    assert "台账里找不到这条记录" in warnings, warnings

    button = next(item for item in at.button if item.label == "清除台账引用（保留节点）")
    button.click().run()
    with env["factory"]() as session:
        node = tree.get_node(session, env["loose"])
    assert not node.ref_type and node.ref_id is None
