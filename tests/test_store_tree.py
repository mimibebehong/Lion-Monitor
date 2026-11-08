"""健康树配置子模块（store.tree）：节点 CRUD、层级校验、级联软删、快照、模板导入导出。"""

from __future__ import annotations

import pytest

from lion_monitor.store import tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "lion.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="卷绕产线振动监测")


def build_winding_line(session, task):
    """建一棵卷绕产线小树：设备 → 对象 → 通道 → 指标。"""
    auto_root = tree.root_node(session, task.id)
    root = tree.update_node(session, auto_root.id, name="卷绕产线振动监测")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    bearing = tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, attrs={"类型": "轴承"}
    )
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=bearing.id)
    metric = tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=channel.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
    )
    return root, device, bearing, channel, metric


# ---------------------------------------------------------------- 建节点


def test_create_root_node_has_own_path(session, task):
    root = tree.create_node(session, task.id, "task", "卷绕产线振动监测")

    assert root.node_type == "task"
    assert root.parent_id is None
    assert root.path == str(root.id)
    assert root.attrs == {}


def test_create_child_appends_path(session, task):
    root = tree.create_node(session, task.id, "task", "卷绕产线振动监测")
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    bearing = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)

    assert device.path == f"{root.id}/{device.id}"
    assert bearing.path == f"{root.id}/{device.id}/{bearing.id}"


def test_create_node_keeps_attrs(session, task):
    root = tree.create_node(session, task.id, "task", "卷绕产线振动监测")
    device = tree.create_node(
        session, task.id, "device", "卷绕产线", parent_id=root.id, attrs={"工位": "WL-1"}
    )

    assert device.attrs == {"工位": "WL-1"}


def test_reject_wrong_parent_type(session, task):
    root = tree.create_node(session, task.id, "task", "卷绕产线振动监测")
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)

    with pytest.raises(tree.TreeError, match="父节点必须是"):
        tree.create_node(session, task.id, "channel", "CH1", parent_id=device.id)


def test_reject_root_with_parent(session, task):
    root = tree.create_node(session, task.id, "task", "卷绕产线振动监测")

    with pytest.raises(tree.TreeError, match="不能有父节点"):
        tree.create_node(session, task.id, "task", "第二个根", parent_id=root.id)


def test_reject_unknown_node_type(session, task):
    with pytest.raises(tree.TreeError, match="节点类型"):
        tree.create_node(session, task.id, "sensor", "CH1")


def test_reject_empty_name(session, task):
    with pytest.raises(tree.TreeError, match="名称不能为空"):
        tree.create_node(session, task.id, "task", "   ")


def test_reject_child_without_existing_parent(session, task):
    with pytest.raises(tree.NodeNotFoundError):
        tree.create_node(session, task.id, "device", "卷绕产线", parent_id=9999)


# ---------------------------------------------------------------- 查 / 改


def test_list_nodes_isolated_by_task(session):
    a = tasks.create_task(session, name="任务A")
    b = tasks.create_task(session, name="任务B")
    tree.create_node(session, a.id, "task", "A 的树")
    tree.create_node(session, b.id, "task", "B 的树")

    assert [node.name for node in tree.list_nodes(session, a.id)] == ["任务A", "A 的树"]
    assert [node.name for node in tree.list_nodes(session, b.id)] == ["任务B", "B 的树"]


def test_update_node_name_and_attrs(session, task):
    root, device, *_ = build_winding_line(session, task)

    updated = tree.update_node(session, device.id, name="卷绕主机", attrs={"工位": "WL-2"})

    assert updated.name == "卷绕主机"
    assert updated.attrs == {"工位": "WL-2"}
    assert updated.path == device.path


# ---------------------------------------------------------------- 删


def test_soft_delete_cascades_to_descendants(session, task):
    root, device, bearing, channel, metric = build_winding_line(session, task)

    counts = tree.soft_delete_node(session, bearing.id)

    assert counts["component"] == 1
    assert counts["channel"] == 1
    assert counts["metric"] == 1
    remaining = [node.name for node in tree.list_nodes(session, task.id)]
    assert remaining == ["卷绕产线振动监测", "卷绕产线"]
    with pytest.raises(tree.NodeNotFoundError):
        tree.get_node(session, metric.id)


def test_soft_delete_keeps_rows_for_restore(session, task):
    root, device, *_ = build_winding_line(session, task)

    tree.soft_delete_node(session, device.id)

    all_nodes = tree.list_nodes(session, task.id, include_deleted=True)
    assert len(all_nodes) == 5
    assert sum(1 for node in all_nodes if node.deleted_at is not None) == 4


# ---------------------------------------------------------------- 快照


def test_snapshot_builds_nested_tree(session, task):
    build_winding_line(session, task)

    snap = tree.tree_snapshot(session, task.id)

    assert snap["name"] == "卷绕产线振动监测"
    assert snap["type"] == "task"
    device = snap["children"][0]
    assert device["name"] == "卷绕产线"
    bearing = device["children"][0]
    assert bearing["attrs"] == {"类型": "轴承"}
    assert bearing["children"][0]["name"] == "CH1"
    assert bearing["children"][0]["children"][0]["name"] == "v_RMS"


# ---------------------------------------------------------------- 模板导入导出


def test_export_template_includes_node_shape(session, task):
    build_winding_line(session, task)

    payload = tree.export_template(session, task.id)

    assert payload["schema"] == "lion-monitor/health-tree"
    assert payload["version"] == 2
    assert payload["root"]["name"] == "卷绕产线振动监测"
    assert payload["root"]["children"][0]["children"][0]["attrs"] == {"类型": "轴承"}


def test_import_template_replaces_whole_tree(session, task):
    build_winding_line(session, task)
    other = tasks.create_task(session, name="包装线监测")
    other_root = tree.root_node(session, other.id)
    tree.update_node(session, other_root.id, name="包装线振动监测")
    device = tree.create_node(session, other.id, "device", "包装机3", parent_id=other_root.id)
    payload = tree.export_template(session, other.id)

    result = tree.import_template(session, task.id, payload)

    assert result.node_counts["device"] == 1
    names = [node.name for node in tree.list_nodes(session, task.id)]
    assert names == ["包装线振动监测", "包装机3"]
    assert "卷绕产线" not in names
    assert device.name == "包装机3"


def test_import_template_rejects_bad_hierarchy(session, task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": "坏树",
            "children": [{"type": "metric", "name": "错层指标", "attrs": {}, "children": []}],
        },
    }

    with pytest.raises(tree.TreeError, match="层级"):
        tree.import_template(session, task.id, payload)
    # 校验失败不落库：只保留任务自带根节点
    assert [node.name for node in tree.list_nodes(session, task.id)] == ["卷绕产线振动监测"]


def test_import_template_rejects_empty_name(session, task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {"type": "task", "name": "  ", "children": []},
    }

    with pytest.raises(tree.TreeError, match="名称不能为空"):
        tree.import_template(session, task.id, payload)


# ---------------------------------------------------------------- 只校验不落库


def test_validate_template_previews_type_counts(session, task):
    build_winding_line(session, task)
    payload = tree.export_template(session, task.id)

    counts = tree.validate_template(payload)

    assert counts == {"task": 1, "device": 1, "component": 1, "channel": 1, "metric": 1}


def test_validate_template_rejects_missing_schema(task):
    with pytest.raises(tree.TreeError, match="模板标识"):
        tree.validate_template(
            {"version": 1, "root": {"type": "task", "name": "x", "children": []}}
        )


def test_validate_template_writes_nothing(session, task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {"type": "task", "name": "新树", "attrs": {}, "children": []},
    }

    tree.validate_template(payload)

    nodes = tree.list_nodes(session, task.id)
    assert [node.node_type for node in nodes] == ["task"]  # 只剩任务自带根节点


def test_child_and_parent_mappings_point_the_right_way():
    assert tree.CHILD_OF == {
        "task": "device",
        "device": "component",
        "component": "channel",
        "channel": "metric",
    }
    assert tree.PARENT_OF["channel"] == "component"
    assert tree.PARENT_OF["task"] is None
