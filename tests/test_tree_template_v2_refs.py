"""健康树模板 v2（T1）：文件头、业务键引用、指标参数随模板走。

v1 的问题（本次要改掉的）：
1. 只导出结构 + attrs + 自增 ref_id，指标参数（params_json / params_locked）全丢；
2. ref 用自增 id，跨库/跨任务导入必然错挂或报「引用不存在」。

v2 口径：引用按业务键（通道号 / 监测对象名称+部位 / 指标码）解析，
metric 节点带 ``params``（覆盖 diff）、``locked``、``rpm``。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from lion_monitor.store import (
    channel_reg,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
    tree_template,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory

PROJECTED = ["卷绕产线", "隔膜过辊轴承", "CH1"]


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "tpl_v2.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def _ledger(session, task_id: int) -> dict[str, Any]:
    """同一物理产线在新任务里的台账：对象 + 2 个通道 + 内置三指标。"""
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    ch2 = channel_reg.create_channel(session, task_id, obj.id, "CH2")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "ch2": ch2, "defs": defs}


def build_line(session, task_id: int, refs: dict[str, Any]) -> dict[str, int]:
    """设备 → 监测对象（带转速）→ CH1/CH2 → 指标节点，并给 CH1 绑两个指标参数。"""
    root = tree.root_node(session, task_id)
    device = tree.create_node(
        session, task_id, "device", "卷绕产线", parent_id=root.id, attrs={"工位": "WL-1"}
    )
    comp = tree.create_node(
        session,
        task_id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"类型": "轴承", "部位": "隔膜过辊", "转速": "1500"},
        ref_id=refs["object"].id,
    )
    ch_nodes = {}
    for code in ("CH1", "CH2"):
        ch_nodes[code] = tree.create_node(
            session, task_id, "channel", code, parent_id=comp.id, ref_id=refs[f"ch{code[-1]}"].id
        )
    metric_nodes = {}
    for code in ("v_RMS", "a_Peak"):
        metric_nodes[code] = tree.create_node(
            session,
            task_id,
            "metric",
            code,
            parent_id=ch_nodes["CH1"].id,
            attrs={"code": code, "单位": refs["defs"][code].unit},
            ref_id=refs["defs"][code].id,
        )
    # v_RMS 只覆盖带通上限（未固化）；a_Peak 固化并把去直流关掉
    metric_assign.assign_metric(
        session, task_id, ch_nodes["CH1"].id, "v_RMS", params={"band_high": 2000}
    )
    metric_assign.assign_metric(
        session,
        task_id,
        ch_nodes["CH1"].id,
        "a_Peak",
        params={"detrend": False},
        params_locked=True,
    )
    return {
        "device": device.id,
        "component": comp.id,
        "CH1": ch_nodes["CH1"].id,
        "CH2": ch_nodes["CH2"].id,
        **{code: row.id for code, row in metric_nodes.items()},
    }


@pytest.fixture()
def source(session):
    """源任务：完整台账 + 整棵树 + 指标参数。"""
    task = tasks.create_task(session, name="卷绕产线振动监测")
    refs = _ledger(session, task.id)
    ids = build_line(session, task.id, refs)
    return {"task": task, "refs": refs, "ids": ids}


@pytest.fixture()
def target(session):
    """目标任务：同名台账（id 与源任务完全不同）。"""
    task = tasks.create_task(session, name="新任务")
    refs = _ledger(session, task.id)
    return {"task": task, "refs": refs}


def walk(node: dict[str, Any]):
    yield node
    for child in node.get("children") or []:
        yield from walk(child)


def node_by_name(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for node in walk(payload["root"]):
        if node["name"] == name:
            return node
    raise AssertionError(f"模板里没有节点 {name}")


def bindings(session, task_id: int) -> dict[tuple[str, str], tuple[bool, dict[str, Any]]]:
    """(监测对象/通道路径, 指标码) → (locked, 生效参数)。"""
    nodes = {row.id: row for row in tree.list_nodes(session, task_id)}
    out: dict[tuple[str, str], tuple[bool, dict[str, Any]]] = {}
    for row in metric_assign.list_assignments(session, task_id):
        channel = nodes.get(row.node_id)
        if channel is None:
            continue
        parent = nodes.get(channel.parent_id or 0)
        key = (f"{parent.name if parent else ''}/{channel.name}", row.code)
        out[key] = (row.params_locked, metric_assign.effective_params(session, row.id))
    return out


# ---------------------------------------------------------------- T1-1 文件头


def test_export_header_is_v2_with_iso_timestamp_and_task_name(session, source):
    payload = tree.export_template(session, source["task"].id)

    assert payload["schema"] == "lion-monitor/health-tree"
    assert payload["version"] == 2
    assert payload["task"] == "卷绕产线振动监测"
    stamp = dt.datetime.fromisoformat(payload["exported_at"])
    assert stamp.year >= 2026
    assert payload["root"]["type"] == "task"


def test_export_keeps_node_structure_and_attrs(session, source):
    before = tree.tree_snapshot(session, source["task"].id)
    payload = tree.export_template(session, source["task"].id)

    def shape(node, out):
        out.append((node["type"], node["name"], dict(node.get("attrs") or {})))
        for child in node.get("children") or []:
            shape(child, out)
        return out

    assert shape(payload["root"], []) == shape(before, [])


# ---------------------------------------------------------------- T1-2 业务键引用


def test_export_references_by_business_key_not_auto_increment_id(session, source):
    payload = tree.export_template(session, source["task"].id)

    comp = node_by_name(payload, "隔膜过辊轴承")
    ch1 = node_by_name(payload, "CH1")
    v_rms = node_by_name(payload, "v_RMS")

    assert comp["object"] == {"name": "隔膜过辊轴承", "part": "隔膜过辊"}
    assert ch1["channel"] == "CH1"
    assert v_rms["metric"] == "v_RMS"
    for node in walk(payload["root"]):
        assert "ref_id" not in node, node["name"]
        assert "ref_type" not in node, node["name"]


def test_export_has_no_id_fields_at_all(session, source):
    payload = tree.export_template(session, source["task"].id)
    assert all("id" not in node for node in walk(payload["root"]))


# ---------------------------------------------------------------- T1-3 指标参数


def test_export_metric_node_carries_params_locked_and_rpm(session, source):
    payload = tree.export_template(session, source["task"].id)

    v_rms = node_by_name(payload, "v_RMS")
    a_peak = node_by_name(payload, "a_Peak")

    assert v_rms["bound"] is True
    assert v_rms["params"] == {"band_high": 2000}
    assert v_rms["locked"] is False
    assert v_rms["rpm"] == 1500.0
    assert a_peak["bound"] is True
    assert a_peak["params"] == {"detrend": False}
    assert a_peak["locked"] is True
    assert a_peak["attrs"] == {"code": "a_Peak", "单位": "g"}


def test_export_unbound_metric_node_is_marked_unbound(session, source):
    ch2 = source["ids"]["CH2"]
    refs = source["refs"]
    tree.create_node(
        session,
        source["task"].id,
        "metric",
        "a_RMS",
        parent_id=ch2,
        attrs={"code": "a_RMS", "单位": "g"},
        ref_id=refs["defs"]["a_RMS"].id,
    )

    payload = tree.export_template(session, source["task"].id)

    a_rms = node_by_name(payload, "a_RMS")
    assert a_rms["bound"] is False
    assert a_rms["params"] == {}
    assert a_rms["locked"] is False


# ---------------------------------------------------------------- T1-4 跨库导入


def test_import_into_new_task_maps_refs_by_business_key(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    tree.import_template(session, target["task"].id, payload)

    nodes = {row.name: row for row in tree.list_nodes(session, target["task"].id)}
    assert nodes["隔膜过辊轴承"].ref_type == "monitor_object"
    assert nodes["隔膜过辊轴承"].ref_id == target["refs"]["object"].id
    assert nodes["隔膜过辊轴承"].ref_id != source["refs"]["object"].id
    assert nodes["CH1"].ref_id == target["refs"]["ch1"].id
    assert nodes["v_RMS"].ref_id == target["refs"]["defs"]["v_RMS"].id
    assert nodes["v_RMS"].node_type == "metric"
    assert nodes["隔膜过辊轴承"].attrs["转速"] == "1500"


def test_roundtrip_keeps_effective_params_and_locked_flag(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    before = bindings(session, source["task"].id)

    tree.import_template(session, target["task"].id, payload)

    after = bindings(session, target["task"].id)
    assert set(after) == set(before)
    for key, (locked, params) in before.items():
        assert after[key][0] == locked, key
        assert after[key][1] == params, key
        assert metric_assign.diff_params(after[key][1], params) == {}


def test_roundtrip_keeps_tree_level_and_names(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    tree.import_template(session, target["task"].id, payload)

    def outline(session, task_id):
        return [
            (row.node_type, row.name, row.parent_id is None)
            for row in tree.list_nodes(session, task_id)
        ]

    src = outline(session, source["task"].id)
    dst = outline(session, target["task"].id)
    # 整树替换（与 v1 同口径）：根节点名沿用模板里的名字，其余层级/名称/顺序完全一致
    assert dst[0][:2] == ("task", "卷绕产线振动监测")
    assert dst[1:] == src[1:]


# ---------------------------------------------------------------- T1-5 未匹配清单


def test_import_collects_all_unmatched_refs_in_one_error(session, source):
    task = tasks.create_task(session, name="空台账任务")
    payload = tree.export_template(session, source["task"].id)

    with pytest.raises(tree.TreeError) as excinfo:
        tree.import_template(session, task.id, payload)

    message = str(excinfo.value)
    assert "未匹配" in message
    assert "隔膜过辊轴承" in message and "CH1" in message
    assert "v_RMS" in message
    # 一次性报全：三类台账都列出来
    assert message.count("未匹配") >= 1
    assert [row.node_type for row in tree.list_nodes(session, task.id)] == ["task"]


def test_import_does_not_guess_when_only_object_missing(session, source, target):
    object_registry.delete_object(session, target["refs"]["object"].id)
    payload = tree.export_template(session, source["task"].id)

    with pytest.raises(tree.TreeError, match="未匹配"):
        tree.import_template(session, target["task"].id, payload)


def test_unmatched_list_is_exposed_by_preview_without_writing(session, source):
    task = tasks.create_task(session, name="空台账任务")
    payload = tree.export_template(session, source["task"].id)

    preview = tree_template.preview_import(session, task.id, payload)

    assert preview.unmatched
    assert preview.blocked is True
    assert {item.field for item in preview.unmatched} == {"object", "channel", "metric"}
    assert [row.node_type for row in tree.list_nodes(session, task.id)] == ["task"]
