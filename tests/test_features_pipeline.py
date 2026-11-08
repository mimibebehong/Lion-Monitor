"""导入管线测试：目录 → 数据集 → 指标 → 有效性。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261029152834"


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
def channel_nodes(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes = {}
    for code in ("CH1", "CH2"):
        node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
        nodes[code] = node.id
        # 健康树里给每个通道挂上三个指标节点（导入按这里的配置算）
        for metric_code in ("v_RMS", "a_Peak", "a_RMS"):
            tree.create_node(
                session,
                task.id,
                "metric",
                metric_code,
                parent_id=node.id,
                attrs={"code": metric_code},
            )
    return nodes


def _batch_dir(tmp_path, amplitude: float = 1.0) -> object:
    folder = tmp_path / "四通道保存" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = (amplitude * np.sin(2 * np.pi * 100 * t)).astype("float32")
    for ch in (1, 2):
        write_dat(
            folder / f"{STAMP}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def test_import_batch_writes_metrics_and_marks_valid(session, task, channel_nodes, tmp_path):
    result = import_batch(session, task.id, _batch_dir(tmp_path), channel_nodes)

    assert result.status == "有效"
    assert result.channels == 2
    assert result.files_ok == 2
    assert result.metrics_written == 6  # 2 通道 × 3 指标
    assert result.unmapped_channels == []

    values = metric_store.list_values(session, task.id, "CH1")
    assert {v.metric_code for v in values} == {"v_RMS", "a_Peak", "a_RMS"}
    peak = next(v for v in values if v.metric_code == "a_Peak")
    assert peak.value == pytest.approx(1.0, rel=2e-3)


def test_import_batch_flags_dead_channel(session, task, channel_nodes, tmp_path):
    folder = tmp_path / "批次" / STAMP
    good = (np.sin(2 * np.pi * 100 * np.arange(int(FS * 2)) / FS)).astype("float32")
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=good,
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    write_dat(
        folder / f"{STAMP}-CH2_Task1.dat",
        samples=np.zeros(int(FS * 2), dtype="float32"),
        channel="CH2",
        started_at=STAMP,
        sample_rate_hz=FS,
    )

    result = import_batch(session, task.id, folder, channel_nodes)

    # 三态（P1 / S5）：1 好 1 判无效 = 部分有效
    assert result.status == "部分有效"
    assert result.files_ok == 1
    assert result.files_bad_count == 1
    assert result.files_bad[0].channel_code == "CH2"
    assert result.files_bad[0].filename == f"{STAMP}-CH2_Task1.dat"
    assert "近似为零" in result.files_bad[0].reason
    assert result.files_bad[0].damaged is False  # 读出来了，只是判无效


def test_import_batch_reports_unmapped_channel(session, task, tmp_path):
    """健康树里只有 CH1：目录里多出来的 CH2 应报「未映射」。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for metric_code in ("v_RMS", "a_Peak", "a_RMS"):
        tree.create_node(
            session, task.id, "metric", metric_code, parent_id=ch1.id, attrs={"code": metric_code}
        )

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1.id})

    assert "CH2" in result.unmapped_channels
    assert result.metrics_written == 3


def test_pipeline_does_not_own_dataset_cascade():
    """级联统一在 store/datasets.py：pipeline 不再提供 purge_dataset 这类删除入口。"""
    from lion_monitor.features import pipeline

    assert not hasattr(pipeline, "purge_dataset")


def test_delete_dataset_removes_metrics(session, task, channel_nodes, tmp_path):
    result = import_batch(session, task.id, _batch_dir(tmp_path), channel_nodes)
    counts = datasets.delete_dataset(session, result.dataset_id)

    assert counts["metric_value"] > 0
    assert metric_store.list_values(session, task.id, "CH1") == []
    assert datasets.list_datasets(session, task.id) == []
