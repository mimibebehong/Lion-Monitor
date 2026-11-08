"""数据集台账（dataset / dataset_file）测试。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features import overview
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.datasets import DatasetError
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


def _make_dir(tmp_path, stamp: str = "20261029152834", channels=(1, 2)) -> object:
    folder = tmp_path / "四通道保存" / stamp
    samples = np.sin(2 * np.pi * 100 * np.arange(1000) / 32000).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"{stamp}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=stamp,
            sample_rate_hz=32000.0,
        )
    return folder


def test_create_dataset_requires_name(session, task):
    with pytest.raises(DatasetError, match="名称"):
        datasets.create_dataset(session, task.id, "  ", "卷绕", "2026-10-29 15:28:34")


def test_create_and_list_dataset(session, task):
    row = datasets.create_dataset(session, task.id, "15:28 采集", "/mnt/d/x", "2026-10-29 15:28:34")
    rows = datasets.list_datasets(session, task.id)
    assert [r.id for r in rows] == [row.id]
    assert rows[0].status == "待判"
    assert rows[0].file_count == 0


def test_import_from_dir_creates_dataset_and_files(session, task, tmp_path):
    folder = _make_dir(tmp_path)
    row = datasets.import_from_dir(session, task.id, folder)

    files = datasets.list_files(session, row.id)
    assert len(files) == 2
    assert [f.channel_code for f in files] == ["CH1", "CH2"]
    assert all(f.sample_rate == 32000.0 for f in files)
    assert all(f.points == 1000 for f in files)
    assert row.collected_at == "2026-10-29 15:28:34"


def test_import_same_dir_twice_is_idempotent(session, task, tmp_path):
    folder = _make_dir(tmp_path)
    first = datasets.import_from_dir(session, task.id, folder)
    second = datasets.import_from_dir(session, task.id, folder)

    assert first.id == second.id
    assert len(datasets.list_datasets(session, task.id)) == 1
    assert len(datasets.list_files(session, first.id)) == 2


def test_import_missing_dir_is_rejected(session, task, tmp_path):
    with pytest.raises(DatasetError, match="目录不存在"):
        datasets.import_from_dir(session, task.id, tmp_path / "没有这个目录")


def test_update_dataset_status_and_remark(session, task, tmp_path):
    row = datasets.import_from_dir(session, task.id, _make_dir(tmp_path))

    updated = datasets.update_dataset(session, row.id, status="有效", remark="幅值正常")
    assert updated.status == "有效"
    assert updated.remark == "幅值正常"

    with pytest.raises(DatasetError, match="状态"):
        datasets.update_dataset(session, row.id, status="说不清")


def test_delete_dataset_cascades_files(session, task, tmp_path):
    row = datasets.import_from_dir(session, task.id, _make_dir(tmp_path))
    datasets.delete_dataset(session, row.id)

    assert datasets.list_datasets(session, task.id) == []
    assert datasets.list_files(session, row.id) == []


def test_delete_dataset_cascades_metric_values_and_over_records(session, task, tmp_path):
    """只走 store 层删除：通道、指标值、超限记录都不再出现在查询结果里。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "metric", "v_RMS", parent_id=ch1.id, attrs={"code": "v_RMS"})
    limits.set_limit(session, task.id, ch1.id, "v_RMS", 2.8, 4.5, ">")

    row = datasets.create_dataset(
        session, task.id, "16:00 采集", tmp_path / "x", "2026-10-29 16:00:00"
    )
    file = datasets.add_file(session, row.id, tmp_path / "x" / "CH1.dat", "CH1", 32000.0, 1000)
    metric_store.upsert_values(
        session, file.id, {"v_RMS": 3.2}, {"v_RMS": "mm/s"}, "2026-10-29 16:00:00"
    )
    assert len(overview.over_records(session, task.id)) == 1

    counts = datasets.delete_dataset(session, row.id)

    assert counts == {"dataset": 1, "channel": 1, "metric_value": 1}
    assert datasets.list_datasets(session, task.id) == []
    assert datasets.list_files(session, row.id) == []
    assert metric_store.list_values(session, task.id, "CH1") == []
    assert overview.over_records(session, task.id) == []


def test_import_dir_without_dat_is_rejected(session, task, tmp_path):
    empty = tmp_path / "空的"
    empty.mkdir()
    with pytest.raises(DatasetError, match="没有 .dat"):
        datasets.import_from_dir(session, task.id, empty)


@pytest.fixture()
def batch_dir(tmp_path):
    return _make_dir(tmp_path)


def test_processed_dirs_maps_source_dir_to_dataset_name(session, task, batch_dir):
    """已处理目录索引：目录 → 数据集名，忽略软删的批次，兼容结尾斜杠。"""
    row = datasets.import_from_dir(session, task.id, batch_dir, name="第一批")

    index = datasets.processed_dirs(session, task.id)

    assert index[datasets.norm_dir(batch_dir)] == row.code
    assert index[datasets.norm_dir(str(batch_dir) + "/")] == row.code


def test_processed_dirs_skips_deleted_datasets(session, task, batch_dir):
    row = datasets.import_from_dir(session, task.id, batch_dir)
    datasets.delete_dataset(session, row.id)

    assert datasets.processed_dirs(session, task.id) == {}
