"""监测对象子模块（store.objects）：组件节点的台账视图与增删改。

监测对象 = task_node 里 node_type=component 的节点，挂在设备下；
这里不做独立表，避免同一实体两处存两份。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import objects, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="卷绕产线振动监测")


@pytest.fixture()
def device(session, task):
    root = tree.root_node(session, task.id)
    return tree.create_node(
        session, task.id, "device", "卷绕产线", parent_id=root.id, attrs={"工位": "WL-1"}
    )


@pytest.fixture()
def bearing(session, task, device):
    comp = objects.create_object(
        session, task.id, device.id, "隔膜过辊轴承", "轴承", part="隔膜过辊"
    )
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.node_id)
    tree.create_node(session, task.id, "metric", "v_RMS", parent_id=ch1.id, attrs={"code": "v_RMS"})
    tree.create_node(
        session, task.id, "metric", "a_Peak", parent_id=ch1.id, attrs={"code": "a_Peak"}
    )
    tree.create_node(session, task.id, "channel", "CH2", parent_id=comp.node_id)
    return comp


# ---------------------------------------------------------------- 台账


def test_list_objects_returns_components_with_counts(session, task, bearing):
    rows = objects.list_objects(session, task.id)

    assert [row.name for row in rows] == ["隔膜过辊轴承"]
    row = rows[0]
    assert row.obj_type == "轴承"
    assert row.part == "隔膜过辊"
    assert row.device_name == "卷绕产线"
    assert row.channel_count == 2
    assert row.metric_count == 2


def test_list_objects_ignores_other_node_types(session, task, bearing):
    rows = objects.list_objects(session, task.id)

    assert all(row.node_id != bearing.node_id or row.name == "隔膜过辊轴承" for row in rows)
    assert len(rows) == 1


def test_list_objects_after_delete_excludes(session, task, bearing):
    objects.delete_object(session, bearing.node_id)

    assert objects.list_objects(session, task.id) == []


# ---------------------------------------------------------------- 新增


def test_create_object_sets_attrs(session, task, device):
    row = objects.create_object(
        session, task.id, device.id, "入片牵引电机", "电机", part="入片牵引"
    )

    node = tree.get_node(session, row.node_id)
    assert node.node_type == "component"
    assert node.attrs["类型"] == "电机"
    assert node.attrs["部位"] == "入片牵引"
    assert row.device_name == "卷绕产线"


def test_create_object_rejects_unknown_type(session, task, device):
    with pytest.raises(objects.ObjectError, match="对象类型"):
        objects.create_object(session, task.id, device.id, "奇怪对象", "齿轮箱")


def test_create_object_requires_device_parent(session, task, device, bearing):
    with pytest.raises(tree.TreeError, match="父节点必须是"):
        objects.create_object(session, task.id, bearing.node_id, "子对象", "电机")


def test_create_object_rejects_empty_name(session, task, device):
    with pytest.raises(tree.TreeError, match="名称"):
        objects.create_object(session, task.id, device.id, "   ", "电机")


# ---------------------------------------------------------------- 修改 / 删除


def test_update_object_changes_name_and_attrs(session, task, bearing):
    row = objects.update_object(
        session, bearing.node_id, name="隔膜过辊轴承-北", obj_type="轴承", part="隔膜过辊北侧"
    )

    assert row.name == "隔膜过辊轴承-北"
    assert row.part == "隔膜过辊北侧"
    assert tree.get_node(session, bearing.node_id).name == "隔膜过辊轴承-北"


def test_update_object_rejects_unknown_type(session, bearing):
    with pytest.raises(objects.ObjectError, match="对象类型"):
        objects.update_object(session, bearing.node_id, obj_type="齿轮箱")


def test_delete_object_cascades_channels_and_metrics(session, task, bearing):
    removed = objects.delete_object(session, bearing.node_id)

    assert removed["component"] == 1
    assert removed["channel"] == 2
    assert removed["metric"] == 2
    assert objects.list_objects(session, task.id) == []
