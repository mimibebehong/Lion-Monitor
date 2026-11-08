"""健康树模板 S3：导出带生效值 + 导入参数三策略 + 预览漂移清单。

问题（复查 S3）：模板只导出 ``params_json``（覆盖 diff），源任务 ``band_high=1000``
（无覆盖）导出成 ``params={}``，导入到定义默认 500 的目标任务后生效值静默变成 500。

本次口径：

- 导出：metric 节点在 ``bound=True`` 时同时写 ``effective_params``
  （该节点实际生效的完整快照）；
- 导入：``ImportOptions.param_strategy`` 三选一 —— ``override``（默认，按生效值搬运）/
  ``locked_only``（只搬运已固化项）/ ``follow_default``（旧行为，只搬覆盖 diff）；
- 预览：``preview_import`` 列出与目标任务默认不同的参数逐项差异
  （目标默认 → 模板生效值），不写库；
- 兼容：v1 照旧可导入；没有 ``effective_params`` 的 v2 文件一律按
  ``follow_default`` 处理并给出提示。
"""

from __future__ import annotations

from typing import Any

import pytest

from lion_monitor.store import (
    bootstrap,
    channel_reg,
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
    engine = make_engine(tmp_path / "tpl_v2_effective.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


# ---------------------------------------------------------------- 夹具


def _ledger(session, task_id: int, band_high: float | None = None) -> dict[str, Any]:
    """台账：1 监测对象 + 1 通道 + 内置三指标；``band_high`` 给定时改该任务的指标定义默认。"""
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch1 = channel_reg.create_channel(session, task_id, obj.id, "CH1")
    metric_def.seed_defaults(session, task_id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    if band_high is not None:
        metric_def.update_metric(session, defs["v_RMS"].id, defaults={"band_high": band_high})
        defs = {row.code: row for row in metric_def.list_metrics(session, task_id)}
    return {"object": obj, "ch1": ch1, "defs": defs}


def _build_line(
    session,
    task_id: int,
    refs: dict[str, Any],
    params: dict[str, Any] | None = None,
    locked: bool = False,
) -> dict[str, int]:
    """设备 → 监测对象 → CH1 → v_RMS 指标节点；``params`` 写进指标绑定。"""
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
        attrs={"部位": "隔膜过辊", "转速": "1500"},
        ref_id=refs["object"].id,
    )
    ch1 = tree.create_node(
        session, task_id, "channel", "CH1", parent_id=comp.id, ref_id=refs["ch1"].id
    )
    metric_node = tree.create_node(
        session,
        task_id,
        "metric",
        "v_RMS",
        parent_id=ch1.id,
        attrs={"code": "v_RMS", "单位": refs["defs"]["v_RMS"].unit},
        ref_id=refs["defs"]["v_RMS"].id,
    )
    metric_assign.assign_metric(
        session, task_id, ch1.id, "v_RMS", params=params or {}, params_locked=locked
    )
    return {"device": device.id, "component": comp.id, "CH1": ch1.id, "v_RMS": metric_node.id}


def _source(session, band_high: float = 1000) -> dict[str, Any]:
    """源任务：定义默认 band_high=1000，节点无覆盖。"""
    task = tasks.create_task(session, name="卷绕产线振动监测")
    refs = _ledger(session, task.id, band_high=band_high)
    _build_line(session, task.id, refs)
    return {"task": task, "refs": refs}


def _target(session, name: str = "新任务", band_high: float = 500) -> dict[str, Any]:
    """目标任务：定义默认 band_high=500。"""
    task = tasks.create_task(session, name=name)
    refs = _ledger(session, task.id, band_high=band_high)
    return {"task": task, "refs": refs}


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _metric_node(payload: dict[str, Any], name: str = "v_RMS") -> dict[str, Any]:
    for node in _walk(payload["root"]):
        if node.get("name") == name and node.get("type") == "metric":
            return node
    raise AssertionError(f"模板里没有指标节点 {name}")


def _effective(session, task_id: int) -> dict[str, Any]:
    """目标任务里 v_RMS 绑定当前的生效参数。"""
    row = next(
        item for item in metric_assign.list_assignments(session, task_id) if item.code == "v_RMS"
    )
    return metric_assign.effective_params(session, row.id)


def _strip_snapshots(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉生效值快照，造一份「旧版 v2」文件（有 v2 外壳但没有生效值快照）。"""
    payload.pop("params_snapshot", None)
    for node in _walk(payload["root"]):
        node.pop("effective_params", None)
    return payload


# ---------------------------------------------------------------- 导出


def test_export_writes_full_effective_params_for_bound_metric(session):
    src = _source(session)

    payload = tree.export_template(session, src["task"].id)

    assert payload["version"] == 2, "仍是 v2 外壳，扩展字段向前兼容"
    assert payload["params_snapshot"] is True
    node = _metric_node(payload)
    assert node["params"] == {}, "节点只存覆盖 diff（旧口径不变）"
    assert node["effective_params"]["band_high"] == pytest.approx(1000.0)
    assert node["effective_params"]["window_s"] == pytest.approx(2.0)
    assert "detrend" in node["effective_params"], "快照必须是完整生效参数，不是 diff"


def test_export_omits_snapshot_for_unbound_metric(session):
    src = _source(session)
    refs = src["refs"]
    ch1 = next(row for row in tree.list_nodes(session, src["task"].id) if row.name == "CH1")
    tree.create_node(
        session,
        src["task"].id,
        "metric",
        "a_RMS",
        parent_id=ch1.id,
        attrs={"code": "a_RMS", "单位": refs["defs"]["a_RMS"].unit},
        ref_id=refs["defs"]["a_RMS"].id,
    )

    payload = tree.export_template(session, src["task"].id)

    a_rms = _metric_node(payload, "a_RMS")
    assert a_rms["bound"] is False
    assert "effective_params" not in a_rms, "没有绑定就没有生效值可写"
    assert _metric_node(payload)["bound"] is True


# ---------------------------------------------------------------- 验收 1 + 2：漂移与预览


def test_override_default_moves_effective_value_across_tasks(session):
    """验收 1：源 1000（无覆盖）→ 导入默认 500 的任务 → 目标生效值 = 1000。"""
    src = _source(session)
    dst = _target(session)
    payload = tree.export_template(session, src["task"].id)

    report = tree.import_template(session, dst["task"].id, payload)

    assert report.version == 2
    assert _effective(session, dst["task"].id)["band_high"] == pytest.approx(1000.0)
    # 落库的是「相对目标默认的 diff」，不是整份快照
    row = next(
        item
        for item in metric_assign.list_assignments(session, dst["task"].id)
        if item.code == "v_RMS"
    )
    assert row.params == {"band_high": 1000.0}


def test_preview_lists_param_drift_before_import(session):
    """验收 2：预览列出 1 处差异，文案含「500 → 1000」。"""
    src = _source(session)
    dst = _target(session)
    payload = tree.export_template(session, src["task"].id)

    preview = tree.preview_import(session, dst["task"].id, payload)

    assert preview.param_diff_count == 1
    only = preview.param_diffs[0]
    assert only.metric_code == "v_RMS"
    assert only.param == "band_high"
    assert only.target_value == pytest.approx(500.0)
    assert only.template_value == pytest.approx(1000.0)
    assert "500 → 1000" in only.text
    assert "CH1" in only.text and "v_RMS" in only.text
    # 预览只读
    assert [row.node_type for row in tree.list_nodes(session, dst["task"].id)] == ["task"]
    assert metric_assign.list_assignments(session, dst["task"].id) == []


def test_preview_without_drift_reports_zero(session):
    """目标默认与源一致时不应误报漂移。"""
    src = _source(session)
    dst = _target(session, band_high=1000)
    payload = tree.export_template(session, src["task"].id)

    preview = tree.preview_import(session, dst["task"].id, payload)

    assert preview.param_diff_count == 0
    assert preview.param_diffs == ()


# ---------------------------------------------------------------- 验收 3：follow_default 保旧行为


def test_follow_default_strategy_keeps_target_default(session):
    """验收 3：follow_default 仍得到 500（旧行为保留）。"""
    src = _source(session)
    dst = _target(session)
    payload = tree.export_template(session, src["task"].id)

    tree.import_template(
        session,
        dst["task"].id,
        payload,
        tree_template.ImportOptions(param_strategy="follow_default"),
    )

    assert _effective(session, dst["task"].id)["band_high"] == pytest.approx(500.0)


def test_locked_only_strategy_moves_only_locked_nodes(session):
    """locked_only：固化节点搬生效值，未固化节点保持旧行为（跟随目标默认）。"""
    locked_src = tasks.create_task(session, name="固化源任务")
    locked_refs = _ledger(session, locked_src.id, band_high=1000)
    _build_line(session, locked_src.id, locked_refs, params={"band_high": 1000}, locked=True)
    dst = _target(session)
    payload = tree.export_template(session, locked_src.id)

    tree.import_template(
        session,
        dst["task"].id,
        payload,
        tree_template.ImportOptions(param_strategy="locked_only"),
    )

    assert _effective(session, dst["task"].id)["band_high"] == pytest.approx(1000.0)
    row = next(
        item
        for item in metric_assign.list_assignments(session, dst["task"].id)
        if item.code == "v_RMS"
    )
    assert row.params_locked is True, "固化标志随模板走"

    unlocked_src = _source(session)
    dst2 = _target(session, name="未固化目标任务")
    payload2 = tree.export_template(session, unlocked_src["task"].id)
    tree.import_template(
        session,
        dst2["task"].id,
        payload2,
        tree_template.ImportOptions(param_strategy="locked_only"),
    )
    assert _effective(session, dst2["task"].id)["band_high"] == pytest.approx(500.0)


def test_override_keeps_locked_flag_from_template(session):
    """override 搬生效值，但不擅自把未固化节点变成固化。"""
    src = _source(session)
    dst = _target(session)
    payload = tree.export_template(session, src["task"].id)

    tree.import_template(session, dst["task"].id, payload)

    row = next(
        item
        for item in metric_assign.list_assignments(session, dst["task"].id)
        if item.code == "v_RMS"
    )
    assert row.params_locked is False


def test_unknown_param_strategy_is_rejected(session):
    with pytest.raises(tree.TreeError, match="参数策略"):
        tree_template.ImportOptions(param_strategy="merge")


# ---------------------------------------------------------------- 验收 4：向前兼容


def test_v1_template_still_imports(session):
    """验收 4（前半）：v1 文件仍可导入（旧自增 id 逻辑不动）。"""
    task = tasks.create_task(session, name="旧模板导入任务")
    _ledger(session, task.id)
    payload = bootstrap.bootstrap_example_tree_payload()

    preview = tree.preview_import(session, task.id, payload)
    report = tree.import_template(session, task.id, payload)

    assert preview.legacy is True
    assert report.version == 1
    assert report.node_counts.get("metric") == 12


def test_v2_without_snapshot_falls_back_to_follow_default_with_notice(session):
    """验收 4（后半）：无 effective_params 的 v2 文件按 follow_default 处理并提示。"""
    src = _source(session)
    dst = _target(session)
    payload = _strip_snapshots(tree.export_template(session, src["task"].id))

    preview = tree.preview_import(session, dst["task"].id, payload)
    assert preview.has_snapshot is False
    assert "生效参数" in preview.notice

    report = tree.import_template(
        session,
        dst["task"].id,
        payload,
        tree_template.ImportOptions(param_strategy="override"),
    )

    assert _effective(session, dst["task"].id)["band_high"] == pytest.approx(500.0)
    assert report.params_overridden == 0
    assert "生效参数" in report.notice


def test_snapshot_template_reports_overridden_count(session):
    """带快照的模板按 override 搬运，报告里给出搬运条数。"""
    src = _source(session)
    dst = _target(session)
    payload = tree.export_template(session, src["task"].id)

    report = tree.import_template(session, dst["task"].id, payload)

    assert report.params_overridden == 1
    assert report.notice == ""


def test_effective_params_must_be_object(session):
    src = _source(session)
    payload = tree.export_template(session, src["task"].id)
    _metric_node(payload)["effective_params"] = [1, 2]

    with pytest.raises(tree.TreeError, match="effective_params"):
        tree.validate_template(payload)
