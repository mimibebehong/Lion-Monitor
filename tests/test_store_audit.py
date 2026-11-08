"""T3 参数 / 阈值变更留痕：写入点统一走 ``store.audit``，只记真正变化的字段。

口径：

- 一条留痕 = 一个字段的一次变化（``entity_type / entity_id / field / old_value / new_value``）
- 参数（dict）变化合成**一条**留痕，old / new 是只含改动键的差异字典
- ``old == new``（数值按 float 比）不写留痕
"""

from __future__ import annotations

import sqlite3

import pytest

from lion_monitor.store import audit, limits, metric_assign, metric_def, tasks, tree
from lion_monitor.store import db as store_db


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "audit.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


def _tree(session) -> dict:
    """任务 → 设备 → 监测对象 → 通道 → 指标节点，返回各 id（任务操作人 mark）。"""
    task = tasks.create_task(session, name="留痕", operator="mark")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric = tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=ch.id,
        attrs={"code": "v_RMS"},
    )
    metric_def.seed_defaults(session, task.id)
    return {"task": task, "ch": ch.id, "metric": metric.id}


def _changes(session, **kwargs) -> list[audit.ChangeRow]:
    return audit.list_changes(session, **kwargs)


# ---------------------------------------------------------------- 阈值


def test_threshold_change_records_only_changed_fields(session):
    ids = _tree(session)
    row = limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)

    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 6.0, 12.0)

    rows = _changes(session, entity_type="metric_limit", entity_id=row.id)
    # 最新两条 = 这次真正的改动（warn 5→6、alert 10→12）
    latest = rows[:2]
    assert {r.field: (r.old_value, r.new_value) for r in latest} == {
        "warn": (5.0, 6.0),
        "alert": (10.0, 12.0),
    }
    assert {r.actor for r in latest} == {"mark"}
    # 方向没变 → 不记
    assert "direction" not in {r.field for r in latest}


def test_threshold_same_values_records_nothing(session):
    ids = _tree(session)
    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)
    before = len(_changes(session))

    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)

    assert len(_changes(session)) == before


def test_threshold_direction_change_is_recorded(session):
    ids = _tree(session)
    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0, ">")

    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 10.0, 5.0, "<")

    rows = [
        row
        for row in _changes(session, entity_type="metric_limit")
        if row.field == "direction" and row.new_value == "<"
    ]
    assert len(rows) == 1
    assert rows[0].old_value == ">"


def test_first_threshold_write_records_from_empty(session):
    ids = _tree(session)
    row = limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)

    rows = _changes(session, entity_type="metric_limit", entity_id=row.id)

    assert {r.field for r in rows} == {"warn", "alert", "direction"}
    assert {r.old_value for r in rows} == {None}


def test_threshold_record_carries_channel_and_metric_in_note(session):
    ids = _tree(session)
    row = limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)

    rows = _changes(session, entity_type="metric_limit", entity_id=row.id)

    assert {r.note for r in rows} == {"CH1 · v_RMS"}


# ---------------------------------------------------------------- 参数（指标授予）


def test_params_override_records_single_row_with_dict_diff(session):
    ids = _tree(session)
    assign = metric_assign.assign_metric(session, ids["task"].id, ids["ch"], "v_RMS")

    metric_assign.update_params(session, assign.id, {"band_high": 500.0})

    rows = _changes(session, entity_type="metric_assign", entity_id=assign.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.field == "params_json"
    assert row.old_value == {"band_high": None}
    assert row.new_value == {"band_high": 500.0}
    assert row.actor == "mark"


def test_params_save_with_same_value_records_nothing(session):
    ids = _tree(session)
    assign = metric_assign.assign_metric(session, ids["task"].id, ids["ch"], "v_RMS")
    metric_assign.update_params(session, assign.id, {"band_high": 500.0})
    before = len(_changes(session))

    metric_assign.update_params(session, assign.id, {"band_high": 500.0})

    assert len(_changes(session)) == before


def test_params_diff_keeps_only_changed_keys(session):
    ids = _tree(session)
    assign = metric_assign.assign_metric(session, ids["task"].id, ids["ch"], "v_RMS")
    metric_assign.update_params(session, assign.id, {"band_high": 500.0, "band_low": 20.0})

    metric_assign.update_params(session, assign.id, {"band_high": 800.0, "band_low": 20.0})

    rows = _changes(session, entity_type="metric_assign", entity_id=assign.id)
    assert rows[0].old_value == {"band_high": 500.0}
    assert rows[0].new_value == {"band_high": 800.0}


def test_set_locked_records_lock_flag_and_snapshot(session):
    ids = _tree(session)
    assign = metric_assign.assign_metric(session, ids["task"].id, ids["ch"], "v_RMS")

    metric_assign.set_locked(session, assign.id, True)

    rows = _changes(session, entity_type="metric_assign", entity_id=assign.id)
    locked = [row for row in rows if row.field == "params_locked"]
    assert len(locked) == 1
    assert (locked[0].old_value, locked[0].new_value) == (False, True)
    # 固化会把全量生效值写进 params_json → 另有一条快照差异
    snapshot = [row for row in rows if row.field == "params_json"]
    assert len(snapshot) == 1
    assert snapshot[0].new_value["band_high"] == 1000.0


# ---------------------------------------------------------------- 指标定义默认参数


def test_metric_def_defaults_change_records_dict_diff(session):
    ids = _tree(session)
    row = metric_def.find_metric(session, ids["task"].id, "v_RMS")
    assert row is not None

    metric_def.update_metric(session, row.id, defaults={**row.defaults, "band_high": 800.0})

    rows = _changes(session, entity_type="metric_def", entity_id=row.id)
    assert [item.field for item in rows] == ["params_default_json"]
    assert rows[0].old_value == {"band_high": 1000}
    assert rows[0].new_value == {"band_high": 800.0}


def test_metric_def_unit_change_records_scalar_field(session):
    ids = _tree(session)
    row = metric_def.find_metric(session, ids["task"].id, "v_RMS")
    assert row is not None

    metric_def.update_metric(session, row.id, unit="mm/s²")

    rows = _changes(session, entity_type="metric_def", entity_id=row.id)
    assert [(item.field, item.old_value, item.new_value) for item in rows] == [
        ("unit", "mm/s", "mm/s²")
    ]


# ---------------------------------------------------------------- 节点转速


def test_node_rpm_change_records_tree_node_row(session):
    ids = _tree(session)

    metric_assign.set_node_rpm(session, ids["ch"], 1480.0)

    rows = _changes(session, entity_type="tree_node", entity_id=ids["ch"])
    assert [(row.field, row.old_value, row.new_value) for row in rows] == [("转速", None, 1480.0)]


def test_node_rpm_same_value_records_nothing(session):
    ids = _tree(session)
    metric_assign.set_node_rpm(session, ids["ch"], 1480.0)
    before = len(_changes(session))

    metric_assign.set_node_rpm(session, ids["ch"], 1480.0)

    assert len(_changes(session)) == before


# ---------------------------------------------------------------- 查询


def test_list_changes_is_newest_first_and_limited(session):
    ids = _tree(session)
    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)
    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 6.0, 12.0)

    rows = _changes(session, limit=2)

    assert len(rows) == 2
    assert rows[0].id > rows[1].id
    # 被 limit 截断后只剩下最近一次改动的两条（warn 5→6、alert 10→12）
    assert {(row.field, row.new_value) for row in rows} == {("warn", 6.0), ("alert", 12.0)}


def test_list_changes_filters_by_entity_type(session):
    ids = _tree(session)
    limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)
    assign = metric_assign.assign_metric(session, ids["task"].id, ids["ch"], "v_RMS")
    metric_assign.update_params(session, assign.id, {"band_high": 500.0})

    assigns = _changes(session, entity_type="metric_assign")

    assert assigns
    assert {row.entity_type for row in assigns} == {"metric_assign"}
    assert {row.field for row in assigns} == {"params_json"}


def test_change_value_roundtrip_keeps_numbers_and_dicts(session):
    ids = _tree(session)
    row = limits.set_limit(session, ids["task"].id, ids["metric"], "v_RMS", 5.0, 10.0)

    stored = _changes(session, entity_type="metric_limit", entity_id=row.id)

    assert {item.field: item.new_value for item in stored}["warn"] == 5.0
    assert isinstance({item.field: item.new_value for item in stored}["warn"], float)


# ---------------------------------------------------------------- 老库迁移


def test_init_db_adds_change_log_table_to_legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    engine.dispose()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE change_log")

    engine = store_db.make_engine(path)
    # 老库少一张表 → 迁移预检能看见，并且会先整库备份
    assert any("change_log" in item for item in store_db.pending_migrations(engine))

    store_db.init_db(engine)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(change_log)")}
    assert {
        "id",
        "entity_type",
        "entity_id",
        "field",
        "old_value",
        "new_value",
        "actor",
        "created_at",
        "note",
    } <= columns
    assert list((tmp_path / "backups").glob("lion-*.db"))
    assert store_db.pending_migrations(engine) == []
    engine.dispose()
