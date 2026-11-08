"""健康树节点引用台账（ref_type / ref_id）测试。"""

from __future__ import annotations

import pytest

from lion_monitor.store import (
    channel_reg,
    metric_def,
    tasks,
    tree,
)
from lion_monitor.store import (
    object_registry as reg,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务A")


@pytest.fixture()
def device(session, task):
    root = tree.root_node(session, task.id)
    return tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)


def test_component_node_can_reference_object(session, task, device):
    obj = reg.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")

    node = tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, ref_id=obj.id
    )

    assert node.ref_type == "monitor_object"
    assert node.ref_id == obj.id


def test_channel_node_can_reference_channel(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    node_c = tree.create_node(
        session, task.id, "component", "轴承A", parent_id=device.id, ref_id=obj.id
    )
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")

    node = tree.create_node(session, task.id, "channel", "CH1", parent_id=node_c.id, ref_id=ch.id)

    assert node.ref_type == "channel_reg"
    assert node.ref_id == ch.id


def test_metric_node_can_reference_metric(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    comp = tree.create_node(
        session, task.id, "component", "轴承A", parent_id=device.id, ref_id=obj.id
    )
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id)
    metric = metric_def.create_metric(session, task.id, "v_RMS", unit="mm/s")

    node = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch_node.id, ref_id=metric.id
    )

    assert node.ref_type == "metric_def"
    assert node.ref_id == metric.id


def test_reference_to_missing_row_is_rejected(session, task, device):
    with pytest.raises(tree.TreeError, match="台账"):
        tree.create_node(session, task.id, "component", "X", parent_id=device.id, ref_id=9999)


def test_reference_type_mismatch_is_rejected(session, task, device):
    metric = metric_def.create_metric(session, task.id, "v_RMS")

    with pytest.raises(tree.TreeError, match="台账类型"):
        tree.create_node(
            session,
            task.id,
            "component",
            "X",
            parent_id=device.id,
            ref_type="metric_def",
            ref_id=metric.id,
        )


def test_update_node_can_set_reference(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    node = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)

    updated = tree.update_node(session, node.id, ref_id=obj.id)

    assert (updated.ref_type, updated.ref_id) == ("monitor_object", obj.id)


# ---------------------------------------------------------------- 模板导出导入带引用


def build_referenced_tree(session, task):
    """设备 → 监测对象 → 通道 → 指标，三层都挂台账引用。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    obj = reg.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    comp = tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"类型": "轴承", "部位": "隔膜过辊"},
        ref_id=obj.id,
    )
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id)
    metric = metric_def.create_metric(session, task.id, "v_RMS", unit="mm/s")
    tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=ch_node.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
        ref_id=metric.id,
    )
    return {"object": obj.id, "channel": ch.id, "metric": metric.id}


def _walk(node):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _norm(node):
    """比对用的结构视图：去掉节点 id（导入后会重新分配）。"""
    return {
        "type": node["type"],
        "name": node["name"],
        "attrs": node["attrs"],
        "ref_type": node.get("ref_type", ""),
        "ref_id": node.get("ref_id"),
        "children": [_norm(child) for child in node.get("children") or []],
    }


def test_export_template_uses_business_keys_for_refs(session, task, device):
    build_referenced_tree(session, task)

    payload = tree.export_template(session, task.id)

    by_type = {node["type"]: node for node in _walk(payload["root"])}
    assert by_type["component"]["object"] == {"name": "隔膜过辊轴承", "part": "隔膜过辊"}
    assert by_type["channel"]["channel"] == "CH1"
    assert by_type["metric"]["metric"] == "v_RMS"
    # 自增 id 不再出现在模板里（跨库导入靠业务键）
    assert all("ref_id" not in node for node in _walk(payload["root"]))
    # 任务/设备没有台账，导出为空中性值（无业务键）
    assert "object" not in by_type["task"]
    assert "object" not in by_type["device"]


def test_export_then_import_roundtrip_keeps_structure_and_refs(session, task, device):
    build_referenced_tree(session, task)
    before = _norm(tree.tree_snapshot(session, task.id))
    payload = tree.export_template(session, task.id)

    tree.import_template(session, task.id, payload)

    after = _norm(tree.tree_snapshot(session, task.id))
    assert after == before


def test_import_template_restores_ledger_references(session, task, device):
    refs = build_referenced_tree(session, task)
    payload = tree.export_template(session, task.id)

    tree.import_template(session, task.id, payload)

    live = {row.node_type: row for row in tree.list_nodes(session, task.id)}
    assert (live["component"].ref_type, live["component"].ref_id) == (
        "monitor_object",
        refs["object"],
    )
    assert (live["channel"].ref_type, live["channel"].ref_id) == ("channel_reg", refs["channel"])
    assert (live["metric"].ref_type, live["metric"].ref_id) == ("metric_def", refs["metric"])


def test_validate_template_rejects_reference_without_id(task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": "坏引用",
            "children": [
                {
                    "type": "device",
                    "name": "设备",
                    "children": [
                        {
                            "type": "component",
                            "name": "轴承",
                            "ref_type": "monitor_object",
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }

    with pytest.raises(tree.TreeError, match="引用"):
        tree.validate_template(payload)


def test_validate_template_rejects_reference_type_mismatch(task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": "坏类型",
            "children": [
                {
                    "type": "device",
                    "name": "设备",
                    "children": [
                        {
                            "type": "component",
                            "name": "轴承",
                            "ref_type": "channel_reg",
                            "ref_id": 1,
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }

    with pytest.raises(tree.TreeError, match="台账类型"):
        tree.validate_template(payload)


def test_import_template_rejects_reference_missing_in_ledger(session, task):
    payload = {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": "悬空引用",
            "children": [
                {
                    "type": "device",
                    "name": "设备",
                    "children": [
                        {
                            "type": "component",
                            "name": "轴承",
                            "attrs": {},
                            "ref_type": "monitor_object",
                            "ref_id": 9999,
                            "children": [],
                        }
                    ],
                }
            ],
        },
    }

    with pytest.raises(tree.TreeError, match="台账"):
        tree.import_template(session, task.id, payload)


def test_snapshot_exposes_reference(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id, ref_id=obj.id)

    snapshot = tree.tree_snapshot(session, task.id)

    def walk(node):
        yield node
        for child in node.get("children", []):
            yield from walk(child)

    component = next(n for n in walk(snapshot) if n["type"] == "component")

    assert (component["ref_type"], component["ref_id"]) == ("monitor_object", obj.id)
