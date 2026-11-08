"""测试任务模块（store.tasks）：建库建表、任务 CRUD、软删除、默认阈值种子。

G5 回归：建任务不再向旧 ``threshold`` 表播种（见
``test_create_task_does_not_seed_legacy_thresholds``；旧表是历史遗留，无人读）。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import inspect, select

from lion_monitor.store import tasks
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import Threshold


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "lion.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def test_init_db_creates_all_tables(tmp_path):
    engine = make_engine(tmp_path / "lion.db")
    init_db(engine)

    names = set(inspect(engine).get_table_names())

    assert {"test_task", "dataset", "channel", "metric_value", "threshold", "deletion_log"} <= names


def test_create_task_generates_code_and_defaults(session):
    task = tasks.create_task(session, name="卷绕产线振动监测", operator="mark")

    assert task.code == f"TT-{date.today():%Y%m%d}-01"
    assert task.name == "卷绕产线振动监测"
    assert task.status == "draft"
    assert task.operator == "mark"
    assert task.created_at is not None
    assert task.deleted_at is None


def test_create_task_increments_daily_sequence(session):
    first = tasks.create_task(session, name="任务A")
    second = tasks.create_task(session, name="任务B")

    assert first.code.endswith("-01")
    assert second.code.endswith("-02")


def test_create_task_rejects_empty_name(session):
    with pytest.raises(tasks.TaskError, match="名称不能为空"):
        tasks.create_task(session, name="   ")


def test_create_task_does_not_seed_legacy_thresholds(session):
    """旧 ``threshold`` 表是历史遗留：建任务不再播种（G5）。"""
    task = tasks.create_task(session, name="卷绕产线振动监测")

    rows = session.execute(select(Threshold).where(Threshold.task_id == task.id)).scalars().all()

    assert rows == []

    second = tasks.create_task(session, name="另一个任务")
    rows2 = session.execute(select(Threshold).where(Threshold.task_id == second.id)).scalars().all()
    assert rows2 == []


def test_list_tasks_newest_first_and_hides_deleted(session):
    a = tasks.create_task(session, name="任务A")
    b = tasks.create_task(session, name="任务B")
    tasks.soft_delete_task(session, a.id)

    codes = [row.code for row in tasks.list_tasks(session)]

    assert codes == [b.code]
    assert len(tasks.list_tasks(session, include_deleted=True)) == 2


def test_get_task_by_id_and_code(session):
    task = tasks.create_task(session, name="任务A")

    assert tasks.get_task(session, task.id).code == task.code
    assert tasks.get_task_by_code(session, task.code).id == task.id


def test_get_task_missing_raises(session):
    with pytest.raises(tasks.TaskNotFoundError):
        tasks.get_task(session, 999)


def test_update_task_changes_fields(session):
    task = tasks.create_task(session, name="任务A")

    updated = tasks.update_task(session, task.id, name="任务A（改名）", remark="补充说明")

    assert updated.name == "任务A（改名）"
    assert updated.remark == "补充说明"
    assert updated.status == "draft"


def test_update_task_rejects_unknown_status(session):
    task = tasks.create_task(session, name="任务A")

    with pytest.raises(tasks.TaskError, match="状态"):
        tasks.update_task(session, task.id, status="flying")


def test_archive_task_sets_status_and_keeps_row(session):
    task = tasks.create_task(session, name="任务A")

    archived = tasks.archive_task(session, task.id)

    assert archived.status == "archived"
    assert tasks.get_task(session, task.id).archived is True


def test_soft_delete_task_writes_deletion_log(session):
    task = tasks.create_task(session, name="任务A")

    deleted = tasks.soft_delete_task(session, task.id, operator="mark")

    assert deleted.deleted_at is not None
    logs = tasks.list_deletion_logs(session)
    assert len(logs) == 1
    assert logs[0].entity_type == "test_task"
    assert logs[0].entity_id == task.id
    assert logs[0].mode == "soft"
    assert logs[0].operator == "mark"


def test_soft_deleted_task_cannot_be_updated(session):
    task = tasks.create_task(session, name="任务A")
    tasks.soft_delete_task(session, task.id)

    with pytest.raises(tasks.TaskNotFoundError):
        tasks.update_task(session, task.id, name="改不动")


def test_task_usage_counts_zero_for_fresh_task(session):
    task = tasks.create_task(session, name="任务A")

    usage = tasks.task_usage(session, task.id)

    assert usage == {"dataset": 0, "channel": 0, "metric_value": 0}


def test_pick_task_returns_requested_task(session):
    first = tasks.create_task(session, name="任务A")
    second = tasks.create_task(session, name="任务B")

    picked = tasks.pick_task(tasks.list_tasks(session), first.id)

    assert picked is not None
    assert picked.id == first.id
    assert picked.id != second.id


def test_pick_task_falls_back_to_newest(session):
    tasks.create_task(session, name="任务A")
    newest = tasks.create_task(session, name="任务B")

    assert tasks.pick_task(tasks.list_tasks(session), 9999).id == newest.id
    assert tasks.pick_task(tasks.list_tasks(session), None).id == newest.id


def test_pick_task_returns_none_when_empty(session):
    assert tasks.pick_task([], 1) is None


def test_create_task_also_creates_root_node(session):
    from lion_monitor.store import tree

    row = tasks.create_task(session, name="新任务")

    root = tree.root_node(session, row.id)
    assert root.node_type == "task"
    assert root.name == "新任务"
    assert root.parent_id is None


def test_rename_task_renames_root_node(session):
    from lion_monitor.store import tree

    row = tasks.create_task(session, name="旧名字")
    tasks.update_task(session, row.id, name="新名字")

    assert tree.root_node(session, row.id).name == "新名字"
