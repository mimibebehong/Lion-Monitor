"""健康树模板 v2（T3）：导入预览 + 范围选择 + 阈值策略 + 完成报告。

预览要求：本文件含 树节点 N、指标绑定 M、阈值 K；未匹配台账清单（有则阻止导入）。
范围：☑树结构 ☑指标参数 ☑阈值。阈值策略：覆盖 / 只补缺失 / 跳过。
报告：新建节点数、绑定指标数、写阈值数、跳过及原因。
"""

from __future__ import annotations

from typing import Any

import pytest

from lion_monitor.store import (
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
    tree_template,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "tpl_v2_import.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def _ledger(session, task_id: int) -> dict[str, Any]:
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "defs": defs}


def build_line(
    session, task_id: int, refs: dict[str, Any], band_high: float = 2000
) -> dict[str, int]:
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
    metric_assign.assign_metric(session, task_id, ch1.id, "v_RMS", params={"band_high": band_high})
    limits.set_limit(session, task_id, v_rms.id, "v_RMS", 1.8, 2.8)
    return {"device": device.id, "component": comp.id, "CH1": ch1.id, "v_RMS": v_rms.id}


@pytest.fixture()
def source(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    refs = _ledger(session, task.id)
    ids = build_line(session, task.id, refs)
    return {"task": task, "refs": refs, "ids": ids}


@pytest.fixture()
def target(session):
    task = tasks.create_task(session, name="新任务")
    return {"task": task, "refs": _ledger(session, task.id)}


# ---------------------------------------------------------------- 预览


def test_preview_reports_node_metric_threshold_counts(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    preview = tree.preview_import(session, target["task"].id, payload)

    assert preview.node_total == 5
    assert preview.node_counts["metric"] == 1
    assert preview.metric_bindings == 1
    assert preview.threshold_rows == 1
    assert preview.blocked is False
    assert preview.task_name == "卷绕产线振动监测"
    assert preview.version == 2


def test_preview_is_read_only(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    tree.preview_import(session, target["task"].id, payload)

    assert [row.node_type for row in tree.list_nodes(session, target["task"].id)] == ["task"]
    assert metric_assign.list_assignments(session, target["task"].id) == []
    assert limits.list_limits(session, target["task"].id) == []


# ---------------------------------------------------------------- 范围选择


def test_structure_scope_off_keeps_existing_tree_and_updates_params(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    tree.import_template(session, target["task"].id, payload)
    before = [(row.id, row.name) for row in tree.list_nodes(session, target["task"].id)]

    # 源任务改参数后只同步参数，不动树
    metric_assign.update_params(
        session,
        next(
            row.id
            for row in metric_assign.list_assignments(session, source["task"].id)
            if row.code == "v_RMS"
        ),
        {"band_high": 500},
    )
    payload = tree.export_template(session, source["task"].id)
    report = tree.import_template(
        session, target["task"].id, payload, tree_template.ImportOptions(structure=False)
    )

    after = [(row.id, row.name) for row in tree.list_nodes(session, target["task"].id)]
    assert after == before
    assert report.node_total == 0
    assert report.metrics_bound == 1
    row = next(
        item
        for item in metric_assign.list_assignments(session, target["task"].id)
        if item.code == "v_RMS"
    )
    assert row.params["band_high"] == pytest.approx(500)


def test_params_scope_off_writes_no_assignment(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    report = tree.import_template(
        session, target["task"].id, payload, tree_template.ImportOptions(params=False)
    )

    assert report.node_counts["metric"] == 1
    assert report.metrics_bound == 0
    assert metric_assign.list_assignments(session, target["task"].id) == []


def test_thresholds_scope_off_writes_no_limit(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    report = tree.import_template(
        session, target["task"].id, payload, tree_template.ImportOptions(thresholds=False)
    )

    assert report.thresholds_written == 0
    assert limits.list_limits(session, target["task"].id) == []


# ---------------------------------------------------------------- 阈值策略


def test_threshold_strategy_overwrite_replaces_existing(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    tree.import_template(session, target["task"].id, payload)
    nodes = {row.name: row for row in tree.list_nodes(session, target["task"].id)}
    limits.set_limit(session, target["task"].id, nodes["v_RMS"].id, "v_RMS", 9.0, 12.0)

    report = tree.import_template(
        session, target["task"].id, payload, tree_template.ImportOptions(structure=False)
    )

    rows = {row.metric_code: row for row in limits.list_limits(session, target["task"].id)}
    assert report.thresholds_written == 1
    assert (rows["v_RMS"].warn, rows["v_RMS"].alert) == (
        pytest.approx(1.8),
        pytest.approx(2.8),
    )


def test_threshold_strategy_fill_missing_keeps_existing(session, source, target):
    payload = tree.export_template(session, source["task"].id)
    tree.import_template(session, target["task"].id, payload)
    nodes = {row.name: row for row in tree.list_nodes(session, target["task"].id)}
    limits.set_limit(session, target["task"].id, nodes["v_RMS"].id, "v_RMS", 9.0, 12.0)

    report = tree.import_template(
        session,
        target["task"].id,
        payload,
        tree_template.ImportOptions(structure=False, threshold_strategy="fill_missing"),
    )

    rows = {row.metric_code: row for row in limits.list_limits(session, target["task"].id)}
    assert report.thresholds_written == 0
    assert report.thresholds_skipped == 1
    assert any("只补缺失" in text for text in report.skipped)
    assert rows["v_RMS"].warn == pytest.approx(9.0)


def test_threshold_strategy_skip_writes_nothing_and_reports(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    report = tree.import_template(
        session, target["task"].id, payload, tree_template.ImportOptions(threshold_strategy="skip")
    )

    assert report.thresholds_written == 0
    assert report.thresholds_skipped == 1
    assert limits.list_limits(session, target["task"].id) == []
    assert any("跳过" in text for text in report.skipped)


def test_unknown_threshold_strategy_is_rejected(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    with pytest.raises(tree.TreeError, match="策略"):
        tree.import_template(
            session,
            target["task"].id,
            payload,
            tree_template.ImportOptions(threshold_strategy="merge"),
        )


# ---------------------------------------------------------------- 完成报告


def test_report_summarises_nodes_metrics_thresholds(session, source, target):
    payload = tree.export_template(session, source["task"].id)

    report = tree.import_template(session, target["task"].id, payload)

    assert report.node_total == 5
    assert report.node_counts == {
        "task": 1,
        "device": 1,
        "component": 1,
        "channel": 1,
        "metric": 1,
    }
    assert report.metrics_bound == 1
    assert report.thresholds_written == 1
    assert report.thresholds_skipped == 0
    assert report.skipped == ()
    assert report.version == 2
