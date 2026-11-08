"""监测对象台账（monitor_object）：独立主数据，健康树之后只做引用。

测试先行，覆盖：建/查/改/软删、同任务重名拦截、从健康树节点迁移进台账。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import object_registry as reg
from lion_monitor.store import tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="卷绕产线振动监测")


def test_create_and_list_object(session, task):
    reg.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")

    rows = reg.list_objects(session, task.id)

    assert [row.name for row in rows] == ["隔膜过辊轴承"]
    assert rows[0].obj_type == "轴承"
    assert rows[0].part == "隔膜过辊"


def test_create_rejects_duplicate_name(session, task):
    reg.create_object(session, task.id, "隔膜过辊轴承", "轴承")

    with pytest.raises(reg.ObjectRegistryError, match="已存在"):
        reg.create_object(session, task.id, "隔膜过辊轴承", "电机")


def test_create_rejects_unknown_type(session, task):
    with pytest.raises(reg.ObjectRegistryError, match="对象类型"):
        reg.create_object(session, task.id, "X", "齿轮箱")


def test_update_object(session, task):
    row = reg.create_object(session, task.id, "轴承A", "轴承")

    updated = reg.update_object(session, row.id, name="轴承B", obj_type="电机", part="入片")

    assert (updated.name, updated.obj_type, updated.part) == ("轴承B", "电机", "入片")


def test_delete_hides_from_list(session, task):
    row = reg.create_object(session, task.id, "轴承A", "轴承")

    reg.delete_object(session, row.id)

    assert reg.list_objects(session, task.id) == []


def test_sync_from_tree_imports_components(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"类型": "轴承", "部位": "隔膜过辊"},
    )
    tree.create_node(
        session,
        task.id,
        "component",
        "入片牵引电机",
        parent_id=device.id,
        attrs={"类型": "电机"},
    )

    created = reg.sync_from_tree(session, task.id)

    assert created == 2
    assert [r.name for r in reg.list_objects(session, task.id)] == ["隔膜过辊轴承", "入片牵引电机"]


def test_sync_from_tree_is_idempotent(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, attrs={"类型": "轴承"}
    )

    reg.sync_from_tree(session, task.id)
    created_again = reg.sync_from_tree(session, task.id)

    assert created_again == 0
    assert len(reg.list_objects(session, task.id)) == 1
