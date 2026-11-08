"""健康树模板 v2（T2）：阈值区段 —— 按「节点路径 + 指标码」寻址。

阈值来自 ``metric_limit`` 现表（不现算），导出可选（勾「包含阈值配置」，默认勾）。
不勾阈值时模板里只有 root，仍然合法可导入。
"""

from __future__ import annotations

from typing import Any

import pytest

from lion_monitor.features.health import point_level
from lion_monitor.store import (
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "tpl_v2_thresholds.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def _ledger(session, task_id: int) -> dict[str, Any]:
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "defs": defs}


def build_line(session, task_id: int, refs: dict[str, Any]) -> dict[str, int]:
    root = tree.root_node(session, task_id)
    device = tree.create_node(session, task_id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(
        session,
        task_id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"部位": "隔膜过辊", "转速": "1500"},
        ref_id=refs["object"].id,
    )
    ch1 = tree.create_node(
        session, task_id, "channel", "CH1", parent_id=comp.id, ref_id=refs["ch1"].id
    )
    v_rms = tree.create_node(
        session,
        task_id,
        "metric",
        "v_RMS",
        parent_id=ch1.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
        ref_id=refs["defs"]["v_RMS"].id,
    )
    metric_assign.assign_metric(session, task_id, ch1.id, "v_RMS", params={"band_high": 2000})
    return {"device": device.id, "component": comp.id, "CH1": ch1.id, "v_RMS": v_rms.id}


@pytest.fixture()
def source(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    refs = _ledger(session, task.id)
    ids = build_line(session, task.id, refs)
    # 指标节点上的阈值（阈值设置页的默认挂载点）
    limits.set_limit(session, task.id, ids["v_RMS"], "v_RMS", 1.8, 2.8)
    # 通道节点上的阈值（验证寻址不是只认指标节点）
    limits.set_limit(session, task.id, ids["CH1"], "a_Peak", 0.9, 1.6)
    return {"task": task, "refs": refs, "ids": ids}


@pytest.fixture()
def target(session):
    task = tasks.create_task(session, name="新任务")
    return {"task": task, "refs": _ledger(session, task.id)}


def _thresholds(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("thresholds") or []


def test_export_includes_threshold_section_addressed_by_path_and_code(session, source):
    payload = tree.export_template(session, source["task"].id, include_thresholds=True)

    rows = {row["metric_code"]: row for row in _thresholds(payload)}
    assert set(rows) == {"v_RMS", "a_Peak"}
    assert rows["v_RMS"]["path"] == ["卷绕产线", "隔膜过辊轴承", "CH1", "v_RMS"]
    assert rows["v_RMS"]["warn"] == pytest.approx(1.8)
    assert rows["v_RMS"]["alert"] == pytest.approx(2.8)
    assert rows["v_RMS"]["direction"] == "upper"
    assert rows["v_RMS"]["enabled"] is True
    # 通道节点上的阈值：路径到通道为止
    assert rows["a_Peak"]["path"] == ["卷绕产线", "隔膜过辊轴承", "CH1"]


def test_export_thresholds_come_from_metric_limit_table(session, source):
    limits.set_limit(session, source["task"].id, source["ids"]["v_RMS"], "v_RMS", 2.5, 4.4)

    payload = tree.export_template(session, source["task"].id)

    stored = {row.metric_code: row for row in limits.list_limits(session, source["task"].id)}
    exported = {row["metric_code"]: row for row in _thresholds(payload)}
    for code, row in stored.items():
        assert exported[code]["warn"] == pytest.approx(row.warn)
        assert exported[code]["alert"] == pytest.approx(row.alert)


def test_export_without_thresholds_has_only_root_and_is_importable(session, source, target):
    payload = tree.export_template(session, source["task"].id, include_thresholds=False)

    assert "thresholds" not in payload
    # params_snapshot 是 S3 的 v2 扩展字段（生效值快照标记），与阈值开关无关
    assert set(payload) == {
        "schema",
        "version",
        "exported_at",
        "task",
        "params_snapshot",
        "root",
    }

    report = tree.import_template(session, target["task"].id, payload)

    assert report.thresholds_written == 0
    assert limits.list_limits(session, target["task"].id) == []


def test_import_restores_thresholds_on_matching_nodes(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    report = tree.import_template(session, target["task"].id, payload)

    assert report.thresholds_written == 2
    rows = {row.metric_code: row for row in limits.list_limits(session, target["task"].id)}
    assert set(rows) == {"v_RMS", "a_Peak"}
    assert (rows["v_RMS"].warn, rows["v_RMS"].alert) == (pytest.approx(1.8), pytest.approx(2.8))
    nodes = {row.name: row for row in tree.list_nodes(session, target["task"].id)}
    assert rows["v_RMS"].node_id == nodes["v_RMS"].id
    assert rows["a_Peak"].node_id == nodes["CH1"].id
    assert rows["v_RMS"].direction == ">"


def test_roundtrip_thresholds_are_equivalent(session, source, target):
    first = tree.export_template(session, source["task"].id)
    tree.import_template(session, target["task"].id, first)

    second = tree.export_template(session, target["task"].id)

    assert _thresholds(second) == _thresholds(first)


def test_import_rejects_threshold_rows_with_warn_not_below_alert(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    payload["thresholds"][0]["warn"] = 3.0
    payload["thresholds"][0]["alert"] = 3.0

    with pytest.raises(tree.TreeError, match="warn"):
        tree.import_template(session, target["task"].id, payload)


def test_import_rejects_unknown_threshold_direction(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    payload["thresholds"][0]["direction"] = "sideways"

    with pytest.raises(tree.TreeError, match="方向"):
        tree.import_template(session, target["task"].id, payload)


def test_import_rejects_threshold_with_unknown_path(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    payload["thresholds"].append(
        {
            "path": ["卷绕产线", "不存在的对象", "CH9"],
            "metric_code": "v_RMS",
            "warn": 1.0,
            "alert": 2.0,
            "direction": "upper",
            "enabled": True,
        }
    )

    with pytest.raises(tree.TreeError, match="阈值"):
        tree.import_template(session, target["task"].id, payload)


def test_preview_counts_threshold_rows(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    preview = tree.preview_import(session, target["task"].id, payload)

    assert preview.threshold_rows == 2
    assert preview.metric_bindings == 1
    assert preview.node_counts == {
        "task": 1,
        "device": 1,
        "component": 1,
        "channel": 1,
        "metric": 1,
    }


def test_lower_direction_is_mapped_back_to_store_marker(session, source, target):
    """方向别名：模板写 upper/lower，库里存 > / <。"""
    payload = tree.export_template(session, source["task"].id)
    payload["thresholds"][0]["direction"] = "lower"
    payload["thresholds"][0]["warn"] = 0.5
    payload["thresholds"][0]["alert"] = 0.2

    tree.import_template(session, target["task"].id, payload)

    rows = {row.metric_code: row for row in limits.list_limits(session, target["task"].id)}
    assert rows["v_RMS"].direction == "<"


def test_lower_threshold_roundtrip_keeps_values_and_grading(session, source, target):
    """下限阈值导出 → 导入新任务 → 数值与判级都与原任务一致（S-1 验收 4）。"""
    source_task = source["task"].id
    limits.set_limit(session, source_task, source["ids"]["v_RMS"], "v_RMS", 3.0, 1.0, "<")

    payload = tree.export_template(session, source_task)
    report = tree.import_template(session, target["task"].id, payload)

    assert report.thresholds_written == 2
    src = {row.metric_code: row for row in limits.list_limits(session, source_task)}
    dst = {row.metric_code: row for row in limits.list_limits(session, target["task"].id)}
    assert (dst["v_RMS"].warn, dst["v_RMS"].alert) == (src["v_RMS"].warn, src["v_RMS"].alert)
    assert (dst["v_RMS"].warn, dst["v_RMS"].alert, dst["v_RMS"].direction) == (3.0, 1.0, "<")

    src_node = source["ids"]["v_RMS"]
    dst_node = next(
        row.id
        for row in tree.list_nodes(session, target["task"].id)
        if row.node_type == "metric" and row.name == "v_RMS"
    )
    src_limit = limits.resolve_limit(session, source_task, src_node, "v_RMS")
    dst_limit = limits.resolve_limit(session, target["task"].id, dst_node, "v_RMS")
    for value in (3.2, 2.5, 0.8):
        assert point_level(value, dst_limit) == point_level(value, src_limit)


def test_import_rejects_lower_row_with_warn_not_above_alert(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    payload["thresholds"][0]["direction"] = "lower"
    payload["thresholds"][0]["warn"] = 1.0
    payload["thresholds"][0]["alert"] = 3.0

    with pytest.raises(tree.TreeError) as err:
        tree.import_template(session, target["task"].id, payload)

    text = str(err.value)
    assert "下限" in text
    assert "warn > alert" in text
