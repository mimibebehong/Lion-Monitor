"""G8 回归：文件头时间不可解析时只降级该文件；整批都读不了时不建数据集、不抛异常。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G8；reports/2026-10-31-review-ingest.txt §I1）：
头部时间字段不可解析（如 ``20261345256000``）→ 扫描阶段抛 ``VibrationFormatError``，
整批在「扫描」就被拒，同目录里正常的文件也进不来。

口径（本卡定）：
- 单个坏头文件 → 该文件进 ``files_bad``（文件名 + 「文件头时间不可解析」），其余文件照常入库
- 整批文件头部都读不了 → 不建数据集、``discarded=True``、不抛异常（页面给一条中文提示），
  与 S-2 已有「整批没留下数据集」口径一致；已导入过的批次再导不删历史数据
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
#: 复查 §I1 用到的坏头时间（月份 13 / 日 45 → strptime 解析不了）
BAD_STAMP = "20261345256000"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务G8")


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


def _signal() -> np.ndarray:
    t = np.arange(int(FS * 2)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


def _write(folder: Path, channel: str, *, started_at: str = STAMP) -> Path:
    return write_dat(
        folder / f"{STAMP}-{channel}_Task1.dat",
        samples=_signal(),
        channel=channel,
        started_at=started_at,
        sample_rate_hz=FS,
    )


def _batch_dir(tmp_path, channels=CHANNELS, *, bad=()) -> Path:
    """造一个批次目录；``bad`` 里的通道写成「文件头时间不可解析」。"""
    folder = tmp_path / "四通道保存" / STAMP
    for ch in channels:
        _write(folder, ch, started_at=BAD_STAMP if ch in bad else STAMP)
    return folder


def _alive_counts(session) -> tuple[int, int]:
    ds = session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count()
    ch = session.query(Channel).filter(Channel.deleted_at.is_(None)).count()
    return ds, ch


def test_one_bad_header_time_only_degrades_that_file(session, task, channel_nodes, tmp_path):
    """3 个正常文件 + 1 个坏头文件：批次成功，坏文件进 files_bad，3 个通道值照常落库。"""
    folder = _batch_dir(tmp_path, bad=("CH4",))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is False
    assert result.dataset_id is not None
    assert result.files_ok == 3
    assert result.metrics_written == 9
    assert result.channels == 3
    assert result.status == "部分有效"

    assert result.files_bad_count == 1
    entry = result.files_damaged[0]
    assert entry.filename == f"{STAMP}-CH4_Task1.dat"
    assert "文件头时间不可解析" in entry.reason
    assert entry.filename not in entry.reason  # 原因里不再重复文件名
    assert entry.damaged is True

    for code in ("CH1", "CH2", "CH3"):
        assert {v.metric_code for v in metric_store.list_values(session, task.id, code)} == {
            "v_RMS",
            "a_Peak",
            "a_RMS",
        }
    assert metric_store.list_values(session, task.id, "CH4") == []


def test_all_bad_header_time_discards_batch(session, task, channel_nodes, tmp_path):
    """全部文件坏头：不建数据集、discarded=True、不抛异常，库内 0 残留。"""
    folder = _batch_dir(tmp_path, channels=("CH1", "CH2", "CH3"), bad=("CH1", "CH2", "CH3"))

    result = import_batch(session, task.id, folder, channel_nodes)  # 不抛异常

    assert result.discarded is True
    assert result.dataset_id is None
    assert result.status == "无效"
    assert result.files_ok == 0
    assert result.metrics_written == 0
    assert result.channels == 0
    assert result.name == folder.name
    assert result.files_bad_count == 3
    assert {item.filename for item in result.files_bad} == {
        f"{STAMP}-{code}_Task1.dat" for code in ("CH1", "CH2", "CH3")
    }
    assert all(item.damaged for item in result.files_bad)
    assert all("文件头时间不可解析" in item.reason for item in result.files_bad)
    assert "读不了" in result.remark

    # 库内 0 残留（没有空壳数据集 / 通道 / 指标值）
    assert datasets.list_datasets(session, task.id) == []
    assert _alive_counts(session) == (0, 0)
    for code in ("CH1", "CH2", "CH3"):
        assert metric_store.list_values(session, task.id, code) == []


def test_reimport_all_bad_header_keeps_existing_dataset(session, task, channel_nodes, tmp_path):
    """已导入过的批次文件此刻全坏头：再导不删数据集、不动历史指标值。"""
    folder = _batch_dir(tmp_path, channels=("CH1", "CH2", "CH3"))
    good = import_batch(session, task.id, folder, channel_nodes)
    assert good.metrics_written == 9
    before_values = len(metric_store.list_values(session, task.id, "CH1"))

    for code in ("CH1", "CH2", "CH3"):
        _write(folder, code, started_at=BAD_STAMP)
    again = import_batch(session, task.id, folder, channel_nodes)

    assert again.discarded is True
    assert again.dataset_id is None
    assert datasets.get_dataset(session, good.dataset_id).status == "有效"
    assert len(metric_store.list_values(session, task.id, "CH1")) == before_values
    assert len(datasets.list_datasets(session, task.id)) == 1


def test_import_from_dir_all_bad_header_still_raises_dataset_error(session, task, tmp_path):
    """store 层的简单入口保持原口径：整个目录读不了 → 中文 DatasetError。"""
    folder = _batch_dir(tmp_path, channels=("CH1", "CH2"), bad=("CH1", "CH2"))

    with pytest.raises(DatasetError, match="读不了"):
        datasets.import_from_dir(session, task.id, folder)

    assert datasets.list_datasets(session, task.id) == []
