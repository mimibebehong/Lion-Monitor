"""健康树装配 → 通道台账归属双向同步（树是唯一真相）。

马哥 2026-11-02：健康树上配置好之后，台账里的装配关系没更新（传感器页还显示自由）。
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
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    path = tmp_path / "sync.db"
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="归属同步")
        obj_a = object_registry.create_object(session, task.id, "入片牵引电机", "电机")
        obj_b = object_registry.create_object(session, task.id, "卷绕张力气缸", "气缸")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp_a = tree.create_node(
            session,
            task.id,
            "component",
            obj_a.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj_a.id,
        )
        comp_b = tree.create_node(
            session,
            task.id,
            "component",
            obj_b.name,
            parent_id=device.id,
            ref_type="monitor_object",
            ref_id=obj_b.id,
        )
        ch = channel_reg.create_channel(session, task.id, None, "CH2")
        ids = {
            "obj_a": obj_a.id,
            "obj_b": obj_b.id,
            "comp_a": comp_a.id,
            "comp_b": comp_b.id,
            "ch": ch.id,
        }
    yield {"factory": factory, **ids}


def _owner(factory, task_id, channel_id):
    with store_db.session_scope(factory) as session:
        rows = channel_reg.list_channels(session, task_id)
    return next(row.object_id for row in rows if row.id == channel_id)


def test_assembling_on_tree_writes_owner_back(env):
    with store_db.session_scope(env["factory"]) as session:
        tree.create_node(
            session,
            1,
            "channel",
            "CH2",
            parent_id=env["comp_a"],
            ref_type="channel_reg",
            ref_id=env["ch"],
        )
    assert _owner(env["factory"], 1, env["ch"]) == env["obj_a"]


def test_moving_channel_node_updates_owner(env):
    with store_db.session_scope(env["factory"]) as session:
        node = tree.create_node(
            session,
            1,
            "channel",
            "CH2",
            parent_id=env["comp_a"],
            ref_type="channel_reg",
            ref_id=env["ch"],
        )
        node_id = node.id
    with store_db.session_scope(env["factory"]) as session:
        tree.reparent_node(session, node_id, env["comp_b"])
    assert _owner(env["factory"], 1, env["ch"]) == env["obj_b"]


def test_multiple_assemblies_keep_current_owner(env):
    with store_db.session_scope(env["factory"]) as session:
        tree.create_node(
            session,
            1,
            "channel",
            "CH2",
            parent_id=env["comp_a"],
            ref_type="channel_reg",
            ref_id=env["ch"],
        )
        tree.create_node(
            session,
            1,
            "channel",
            "CH2",
            parent_id=env["comp_b"],
            ref_type="channel_reg",
            ref_id=env["ch"],
        )
    assert _owner(env["factory"], 1, env["ch"]) == env["obj_a"]


def test_tree_configuration_updates_ledger_object_column(env):
    """监测对象页「通道」列要能看到装配后的通道"""
    with store_db.session_scope(env["factory"]) as session:
        tree.create_node(
            session,
            1,
            "channel",
            "CH2",
            parent_id=env["comp_a"],
            ref_type="channel_reg",
            ref_id=env["ch"],
        )
    with store_db.session_scope(env["factory"]) as session:
        rows = channel_reg.list_channels(session, 1)
        row = next(item for item in rows if item.id == env["ch"])
    assert row.object_name == "入片牵引电机"
