"""S2：指标定义 schema/默认值落库 + 节点只存覆盖项 + 固化开关 + 影响数据集计数。"""

from __future__ import annotations

import json

import pytest

from lion_monitor.params import PARAM_DEFAULTS, PARAM_SCHEMA, default_params, diff_params
from lion_monitor.store import (
    channel_reg,
    datasets,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import (
    db as store_db,
)


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="参数任务")


@pytest.fixture()
def channel_node(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    # 参数卡落点是指标节点（马哥 2026-11-02）：先装配指标，再绑参数
    for code in ("v_RMS", "BPFO_1X"):
        tree.create_node(
            session,
            task.id,
            "metric",
            code,
            parent_id=channel.id,
            attrs={"code": code},
        )
    return channel


def test_metric_def_has_schema_and_defaults_columns(session):
    metric_def.seed_defaults(session, task_id=1)
    rows = {row.code: row for row in metric_def.list_metrics(session, 1)}
    assert rows["v_RMS"].schema == PARAM_SCHEMA["v_RMS"]
    assert rows["v_RMS"].defaults == PARAM_DEFAULTS["v_RMS"]


def test_legacy_metric_def_table_gets_schema_columns(tmp_path):
    """老库（无两列）跑 init_db 自动补齐，幂等。

    补列后还要回填声明：``params_schema_json`` 是后加列，只有 ``DEFAULT '[]'`` 的话
    界面渲染不出参数表单（改不了计算时长）。内置指标 v_RMS 应按 ``PARAM_SCHEMA`` 回填，
    见 ``store_db.migrate_metric_def_schema``；自定义指标码才保持空声明。
    """
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_def ("
            " id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, code VARCHAR(32),"
            " unit VARCHAR(16), params_json TEXT, enabled INTEGER,"
            " created_at DATETIME, deleted_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO metric_def (task_id, code, unit, params_json, enabled)"
            " VALUES (1, 'v_RMS', 'mm/s', '{}', 1)"
        )
    store_db.init_db(engine)
    store_db.init_db(engine)
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(metric_def)")}
        schema_json, defaults_json = conn.exec_driver_sql(
            "SELECT params_schema_json, params_default_json FROM metric_def"
        ).first()
    assert {"params_schema_json", "params_default_json"} <= cols
    assert json.loads(schema_json) == PARAM_SCHEMA["v_RMS"]
    assert json.loads(defaults_json) == PARAM_DEFAULTS["v_RMS"]
    assert float(json.loads(defaults_json)["window_s"]) == 2.0


def test_create_metric_with_schema_and_defaults(session, task):
    row = metric_def.create_metric(
        session,
        task.id,
        "1X_RMS",
        "mm/s",
        schema=[
            {"key": "window_s", "label": "计算时长", "group": "basic", "type": "float",
             "default": 2.0},
        ],
        defaults={"window_s": 2.0},
    )  # fmt: skip
    assert [item["key"] for item in row.schema] == ["window_s"]
    assert row.defaults == {"window_s": 2.0}


def test_update_metric_can_change_definition_defaults(session, task):
    metric_def.seed_defaults(session, task.id)
    row = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    metric_def.update_metric(session, row.id, defaults={"band_high": 1500.0})
    refreshed = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    assert refreshed.defaults == {"band_high": 1500.0}


def test_metric_assign_has_params_locked_column(session, task, channel_node):
    row = metric_assign.assign_metric(session, task.id, channel_node.id, "v_RMS")
    assert row.params_locked is False
    assert metric_assign.list_assignments(session, task.id)[0].params_locked is False


def test_legacy_metric_assign_table_gets_params_locked(tmp_path):
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_assign ("
            " id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, node_id INTEGER NOT NULL,"
            " metric_code VARCHAR(32), unit VARCHAR(16), params_json TEXT,"
            " enabled INTEGER, deleted_at DATETIME)"
        )
    store_db.init_db(engine)
    store_db.init_db(engine)
    with engine.begin() as conn:
        cols = {row[1]: row for row in conn.exec_driver_sql("PRAGMA table_info(metric_assign)")}
    assert "params_locked" in cols and cols["params_locked"][4] == "0"


def test_assign_metric_accepts_metric_def_codes_beyond_builtins(session, task, channel_node):
    """台账里自建的指标也能装配到通道上（S2 起指标不再限于内置三个）。"""
    metric_def.create_metric(session, task.id, "BPFO_1X", "mm/s")
    row = metric_assign.assign_metric(session, task.id, channel_node.id, "BPFO_1X")
    assert row.code == "BPFO_1X"
    assert row.unit == "mm/s"


def test_assign_metric_rejects_code_missing_from_ledger(session, task, channel_node):
    with pytest.raises(metric_assign.MetricAssignError, match="指标码"):
        metric_assign.assign_metric(session, task.id, channel_node.id, "幽灵指标")


def test_effective_params_merges_three_layers(session, task, channel_node):
    metric_def.seed_defaults(session, task.id)
    v_rms = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    metric_def.update_metric(session, v_rms.id, defaults={"band_high": 1500.0, "window_s": 2.0})
    row = metric_assign.assign_metric(
        session, task.id, channel_node.id, "v_RMS", params={"band_high": 2000.0}
    )

    effective = metric_assign.effective_params(session, row.id)

    assert effective["band_high"] == 2000.0  # 节点覆盖赢
    assert effective["window_s"] == 2.0
    assert effective["band_low"] == default_params("v_RMS")["band_low"]


def test_unlocked_node_follows_definition_default_change(session, task, channel_node):
    """定义改默认值 → 未覆盖的节点跟着变。"""
    metric_def.seed_defaults(session, task.id)
    v_rms = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    row = metric_assign.assign_metric(session, task.id, channel_node.id, "v_RMS")

    assert metric_assign.effective_params(session, row.id)["band_high"] == 1000.0
    metric_def.update_metric(session, v_rms.id, defaults={"band_high": 800.0})
    assert metric_assign.effective_params(session, row.id)["band_high"] == 800.0


def test_overridden_key_ignores_definition_default_change(session, task, channel_node):
    metric_def.seed_defaults(session, task.id)
    v_rms = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    row = metric_assign.assign_metric(
        session, task.id, channel_node.id, "v_RMS", params={"band_high": 2000.0}
    )
    metric_def.update_metric(session, v_rms.id, defaults={"band_high": 800.0})
    assert metric_assign.effective_params(session, row.id)["band_high"] == 2000.0


def test_freeze_snapshots_full_params_and_ignores_later_changes(session, task, channel_node):
    """params_locked=1：把当时的全量生效值固化，此后定义改默认值不影响本节点。"""
    metric_def.seed_defaults(session, task.id)
    v_rms = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    row = metric_assign.assign_metric(session, task.id, channel_node.id, "v_RMS")

    frozen = metric_assign.set_locked(session, row.id, True)
    assert frozen.params_locked is True
    assert frozen.params == default_params("v_RMS")  # 全量快照

    metric_def.update_metric(session, v_rms.id, defaults={"band_high": 700.0})
    assert metric_assign.effective_params(session, row.id)["band_high"] == 1000.0


def test_unfreeze_keeps_only_real_overrides(session, task, channel_node):
    metric_def.seed_defaults(session, task.id)
    v_rms = next(r for r in metric_def.list_metrics(session, task.id) if r.code == "v_RMS")
    row = metric_assign.assign_metric(
        session, task.id, channel_node.id, "v_RMS", params={"band_high": 2000.0}
    )
    metric_assign.set_locked(session, row.id, True)
    metric_def.update_metric(session, v_rms.id, defaults={"band_high": 2000.0})

    thawed = metric_assign.set_locked(session, row.id, False)

    assert thawed.params_locked is False
    # 与定义默认相同的项不该留在覆盖里
    assert thawed.params == {}
    assert metric_assign.effective_params(session, row.id)["band_high"] == 2000.0


def test_update_params_stores_diff_only(session, task, channel_node):
    """节点只存覆盖项：UI 传进来的就是 diff，存储层不擅自补全。"""
    metric_def.seed_defaults(session, task.id)
    row = metric_assign.assign_metric(session, task.id, channel_node.id, "v_RMS")

    updated = metric_assign.update_params(session, row.id, {"band_high": 2000.0})

    assert updated.params == {"band_high": 2000.0}
    effective = metric_assign.effective_params(session, row.id)
    assert diff_params(effective, default_params("v_RMS")) == {"band_high": 2000.0}


def test_effective_params_unknown_code_falls_back_to_builtin(session, task, channel_node):
    """指标定义缺失时只合并硬默认 + 覆盖，不炸。"""
    row = metric_assign.assign_metric(
        session, task.id, channel_node.id, "v_RMS", params={"band_high": 1200.0}
    )
    effective = metric_assign.effective_params(session, row.id)
    assert effective["band_high"] == 1200.0
    assert effective["band_low"] == default_params("v_RMS")["band_low"]


def test_count_datasets_for_channel(session, task, tmp_path):
    """「将影响 N 个数据集」的口径：任务下含该通道号的有效批次数量。"""
    for index in (1, 2):
        ds = datasets.create_dataset(
            session, task.id, f"批次{index}", tmp_path / f"d{index}", "2026-10-31 08:00:00"
        )
        datasets.add_file(
            session, ds.id, tmp_path / f"d{index}" / f"CH1_{index}.dat", "CH1", 25600.0, 51200
        )
    other = datasets.create_dataset(
        session, task.id, "批次3", tmp_path / "d3", "2026-10-31 09:00:00"
    )
    datasets.add_file(session, other.id, tmp_path / "d3" / "CH2.dat", "CH2", 25600.0, 51200)

    assert datasets.count_datasets_for_channel(session, task.id, "CH1") == 2
    assert datasets.count_datasets_for_channel(session, task.id, "CH9") == 0


def test_count_datasets_ignores_deleted_dataset(session, task, tmp_path):
    ds = datasets.create_dataset(session, task.id, "批次", tmp_path / "d", "2026-10-31 08:00:00")
    datasets.add_file(session, ds.id, tmp_path / "d" / "CH1.dat", "CH1", 25600.0, 51200)
    datasets.delete_dataset(session, ds.id)
    assert datasets.count_datasets_for_channel(session, task.id, "CH1") == 0


def test_channel_reg_object_link_used_for_rated_rpm_placeholder(session, task):
    """监测对象台账暂无 rated_rpm（S3 落地）；此处只确认对象/通道可用，不做转速推断。"""
    obj = object_registry.create_object(session, task.id, "3#辊压机", "辊")
    channel_reg.create_channel(session, task.id, obj.id, "CH1")
    assert [c.code for c in channel_reg.list_channels(session, task.id)] == ["CH1"]
