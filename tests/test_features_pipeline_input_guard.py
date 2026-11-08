"""B3 回归：坏输入把页面打成 Traceback（未捕获异常）。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · B3；probes I1 / C10）：

- 头部 ``sample_rate`` = NaN / ``scale`` = NaN / 样本含 NaN → 落库时 ``IntegrityError``
  （``channel.sample_rate_hz`` / ``metric_value.value`` 非空），页面只捕获
  ``(DatasetError, VibrationFormatError, ValueError)`` → 红屏
- 同批两个文件同通道号（``-CH1_Task1`` + ``-CH1_Task2``）→ ``IntegrityError: channel``

口径（本批定）：坏文件按 S-2 已有机制降级 ``files_bad``（``damaged=True``），不中断整批；
同批重复通道号在写库前预检，抛明确中文 ``DatasetError``，库内 0 残留。
整批文件头部都不可用（全 NaN 采样率 / 全 NaN scale）时建不出数据集，同样按 S-2 口径
返回 ``discarded=True`` 的结果（页面一条中文提示、库内 0 残留）——P4-3 / G8 起不再抛
``DatasetError``（坏头文件整批降级与「头部时间不可解析」统一成同一条路）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.datasets import DatasetError
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import Channel, Dataset

FS = 32000.0
STAMP = "20261029152834"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "guard.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务B3")


@pytest.fixture()
def channel_nodes(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes = {}
    for code in CHANNELS:
        node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
        nodes[code] = node.id
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


def _clean_signal() -> np.ndarray:
    t = np.arange(int(FS * 2)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


def _batch_dir(tmp_path, *, samples=None, fs: float = FS, scale: float = 1.0) -> Path:
    folder = tmp_path / "四通道保存" / STAMP
    for ch in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=_clean_signal() if samples is None else samples,
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=fs,
            scale=scale,
        )
    return folder


def _alive_counts(session) -> tuple[int, int]:
    """库内存活的 dataset / channel 行数（软删的不算）。"""
    ds = session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count()
    ch = session.query(Channel).filter(Channel.deleted_at.is_(None)).count()
    return ds, ch


def test_nan_samples_do_not_abort_batch_and_leave_no_residue(
    session, task, channel_nodes, tmp_path
):
    """样本含 NaN：整批降级（不留数据集 / 通道 / 指标值），不抛异常。"""
    samples = _clean_signal()
    samples[100] = np.nan
    folder = _batch_dir(tmp_path, samples=samples)

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is True
    assert result.dataset_id is None
    assert result.files_ok == 0
    assert len(result.files_bad) == 4
    assert all(item.damaged for item in result.files_bad)
    assert all("非有限" in item.reason for item in result.files_bad), result.files_bad
    assert _alive_counts(session) == (0, 0)
    assert metric_store.list_values(session, task.id, "CH1") == []


def test_one_nan_file_only_degrades_that_file(session, task, channel_nodes, tmp_path):
    """只有一个文件含 NaN：其余 3 个照常入库，坏文件 0 指标值。"""
    folder = _batch_dir(tmp_path)
    samples = _clean_signal()
    samples[10] = np.inf
    write_dat(
        folder / f"{STAMP}-CH2_Task1.dat",
        samples=samples,
        channel="CH2",
        started_at=STAMP,
        sample_rate_hz=FS,
    )

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_ok == 3
    assert result.metrics_written == 9
    assert len(result.files_damaged) == 1
    assert result.files_damaged[0].channel_code == "CH2"
    assert metric_store.list_values(session, task.id, "CH2") == []
    for code in ("CH1", "CH3", "CH4"):
        assert len(metric_store.list_values(session, task.id, code)) == 3


def test_scale_nan_batch_is_discarded_before_db_write(session, task, channel_nodes, tmp_path):
    """头部 scale = NaN（整批）：不建数据集、discarded=True，库内 0 残留。

    （原来会在 metric_value 写库时炸 ``IntegrityError``，页面直接红屏；P4-3 / G8 起
    整批头部读不了按「整批没留下数据集」返回结果，不再抛 DatasetError）
    """
    folder = _batch_dir(tmp_path, scale=float("nan"))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is True
    assert result.dataset_id is None
    assert all("scale" in item.reason for item in result.files_bad), result.files_bad
    assert _alive_counts(session) == (0, 0)
    assert metric_store.list_values(session, task.id, "CH1") == []


def test_sample_rate_nan_batch_is_discarded_before_db_write(session, task, channel_nodes, tmp_path):
    """头部采样率 = NaN（整批）：不建数据集、discarded=True，原因是中文可读文案。

    （原来 NOT NULL constraint failed: channel.sample_rate_hz）
    """
    folder = _batch_dir(tmp_path, fs=float("nan"))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is True
    assert result.dataset_id is None
    assert all("采样率" in item.reason for item in result.files_bad), result.files_bad
    assert _alive_counts(session) == (0, 0)
    assert metric_store.list_values(session, task.id, "CH1") == []


def test_sample_rate_zero_batch_is_discarded(session, task, channel_nodes, tmp_path):
    """采样率 = 0（整批）：同样在写库前拦住，库内 0 残留。"""
    folder = _batch_dir(tmp_path, fs=0.0)

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is True
    assert result.dataset_id is None
    assert all("采样率" in item.reason for item in result.files_bad), result.files_bad
    assert _alive_counts(session) == (0, 0)


def test_one_bad_scale_only_degrades_that_file(session, task, channel_nodes, tmp_path):
    """只有一个文件 scale = NaN：其余 3 个照常入库，坏文件记进 files_bad。"""
    folder = _batch_dir(tmp_path)
    write_dat(
        folder / f"{STAMP}-CH4_Task1.dat",
        samples=_clean_signal(),
        channel="CH4",
        started_at=STAMP,
        sample_rate_hz=FS,
        scale=float("nan"),
    )

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_ok == 3
    assert result.metrics_written == 9
    assert [item.filename for item in result.files_damaged] == [f"{STAMP}-CH4_Task1.dat"]
    assert "scale" in result.files_damaged[0].reason
    assert metric_store.list_values(session, task.id, "CH4") == []


def test_duplicate_channel_codes_rejected_before_any_write(session, task, channel_nodes, tmp_path):
    """同批两个文件同通道号：写库前预检，抛中文错误，库内 0 残留。"""
    folder = tmp_path / "四通道保存" / STAMP
    for name in (f"{STAMP}-CH1_Task1.dat", f"{STAMP}-CH1_Task2.dat", f"{STAMP}-CH2_Task1.dat"):
        write_dat(
            folder / name,
            samples=_clean_signal(),
            channel=name.split("-")[1].split("_")[0],
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    with pytest.raises(DatasetError) as err:
        import_batch(session, task.id, folder, channel_nodes)

    message = str(err.value)
    assert "通道号" in message
    assert "CH1" in message
    assert _alive_counts(session) == (0, 0)
    assert datasets.list_datasets(session, task.id) == []
    assert metric_store.list_values(session, task.id, "CH1") == []


def test_unreadable_header_degrades_to_files_bad(session, task, channel_nodes, tmp_path):
    """一个文件头时间解析不了：该文件降级 files_bad，其余 3 个照常入库。"""
    folder = _batch_dir(tmp_path)
    write_dat(
        folder / f"{STAMP}-CH3_Task1.dat",
        samples=_clean_signal(),
        channel="CH3",
        started_at="NOT-A-TIME",
        sample_rate_hz=FS,
    )

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_ok == 3
    assert result.metrics_written == 9
    assert len(result.files_damaged) == 1
    assert result.files_damaged[0].filename == f"{STAMP}-CH3_Task1.dat"
    assert metric_store.list_values(session, task.id, "CH3") == []
