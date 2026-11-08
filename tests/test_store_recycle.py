"""回收站：deletion_log 读取 + 恢复。

覆盖：软删数据集写留痕（含级联计数）、回收站列表读真实留痕、
恢复把 deleted_at 置空并让数据重新可见、恢复不存在的记录只给提示不抛异常。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.store import datasets, metrics, recycle, tasks
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import DeletionLog


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务A")


def _batch_dir(tmp_path: Path, channels=(1, 2)) -> Path:
    folder = tmp_path / "四通道保存" / "20261029152834"
    samples = np.sin(2 * np.pi * 100 * np.arange(1000) / 32000).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"20261029152834-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at="20261029152834",
            sample_rate_hz=32000.0,
        )
    return folder


@pytest.fixture()
def batch(session, task, tmp_path):
    """一个 2 通道数据集，每个通道各 2 个指标值。"""
    folder = _batch_dir(tmp_path)
    row = datasets.import_from_dir(session, task.id, folder)
    for f in datasets.list_files(session, row.id):
        metrics.upsert_values(
            session,
            f.id,
            {"v_RMS": 1.0, "a_Peak": 2.0},
            {"v_RMS": "mm/s", "a_Peak": "g"},
            "2026-10-29 15:28:34",
        )
    return row


def test_delete_dataset_writes_soft_deletion_log(session, task, batch):
    counts = datasets.delete_dataset(session, batch.id, operator="小智")

    logs = tasks.list_deletion_logs(session)
    assert len(logs) == 1
    log = logs[0]
    assert log.entity_type == "dataset"
    assert log.entity_id == batch.id
    assert log.entity_name == f"{batch.code} {batch.name}"
    assert log.mode == "soft"
    assert log.operator == "小智"
    assert counts == {"dataset": 1, "channel": 2, "metric_value": 4}


def test_delete_dataset_keeps_metric_values_from_list_values(session, task, batch):
    datasets.delete_dataset(session, batch.id)

    assert metrics.list_values(session, task.id, "CH1") == []


def test_list_recycle_items_reads_real_deletion_log(session, task, batch):
    datasets.delete_dataset(session, batch.id, operator="小智")

    items = recycle.list_recycle_items(session)

    assert [item.entity_id for item in items] == [batch.id]
    item = items[0]
    assert item.entity_type == "dataset"
    assert item.entity_label == "数据集"
    assert item.entity_name == f"{batch.code} {batch.name}"
    assert item.operator == "小智"
    assert item.deleted_at is not None
    assert item.cascade == {"dataset": 1, "channel": 2, "metric_value": 4}


def test_restore_dataset_clears_deleted_at_and_restores_children(session, task, batch):
    datasets.delete_dataset(session, batch.id)
    item = recycle.list_recycle_items(session)[0]

    result = recycle.restore_item(session, item.log_id)

    assert result.ok is True
    assert "已恢复" in result.message
    rows = datasets.list_datasets(session, task.id)
    assert [r.id for r in rows] == [batch.id]
    assert len(datasets.list_files(session, batch.id)) == 2
    assert len(metrics.list_values(session, task.id, "CH1")) == 2
    assert recycle.list_recycle_items(session) == []


def test_restore_soft_deleted_task_makes_it_visible(session, task):
    tasks.soft_delete_task(session, task.id, operator="小智")
    item = recycle.list_recycle_items(session)[0]
    assert item.entity_type == "test_task"
    assert item.entity_label == "任务"

    result = recycle.restore_item(session, item.log_id)

    assert result.ok is True
    assert [r.id for r in tasks.list_tasks(session)] == [task.id]


def test_restore_unknown_log_reports_message_without_raising(session):
    result = recycle.restore_item(session, 9999)

    assert result.ok is False
    assert "不存在" in result.message


def test_restore_already_alive_item_reports_message(session, task, batch):
    datasets.delete_dataset(session, batch.id)
    item = recycle.list_recycle_items(session)[0]
    recycle.restore_item(session, item.log_id)

    again = recycle.restore_item(session, item.log_id)

    assert again.ok is False
    assert "无需恢复" in again.message


def test_hard_mode_log_is_not_listed_and_not_restorable(session, task):
    session.add(
        DeletionLog(
            entity_type="test_task",
            entity_id=task.id,
            entity_name="已彻底删除",
            mode="hard",
            cascade_counts="{}",
            operator="小智",
        )
    )
    session.commit()

    assert recycle.list_recycle_items(session) == []
    result = recycle.restore_item(session, tasks.list_deletion_logs(session)[0].id)
    assert result.ok is False
    assert "彻底删除" in result.message
