"""S-2 回归测试：批次里个别文件损坏时，不中断整批、不留空数据集。

缺陷背景（docs/reviews/2026-10-31-现场数据实跑复查.md · S-2 / probes P8）：
``import_batch`` 里 ``read_vibration(f.path)`` 没有异常保护，任一文件读失败直接抛出，
该批次其余有效文件都不落库，而 ``import_from_dir`` 已建的数据集记录留下来
（状态「待判」、1 个文件、0 个指标值）→ 库里留垃圾。

口径（本轮定）：
- 单文件读取失败降级为「该文件无效」，写进 ``ImportResult.files_bad``（文件名 + 原因）后 continue
- 整批一个有效文件都没有 → 数据集不保留（软删留痕，不留「待判 + 0 值」空壳）
- 全部文件正常 → 结果与改造前完全一致（指标值逐位相同）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.metrics import compute_metric_series
from lion_monitor.features.pipeline import BadFile, import_batch
from lion_monitor.ingest.vibration import read_vibration
from lion_monitor.store import datasets, recycle, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261029152834"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务S2")


@pytest.fixture()
def channel_nodes(session, task):
    """健康树：设备 → 监测对象 → CH1~CH4 通道节点，每个通道下挂三个指标节点。"""
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


def _batch_dir(tmp_path) -> Path:
    """造一个四通道批次（每通道 2 s / 32 kHz 正弦，幅值 1 g，恰好一窗）。"""
    folder = tmp_path / "四通道保存" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = np.sin(2 * np.pi * 100 * t).astype("float32")
    for ch in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=samples,
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def _truncate(path, ratio: float = 3) -> None:
    """把数据体截掉尾部（头部 512 B 保持完整 → 长度与头部声明不符）。"""
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // ratio])


def test_truncated_file_does_not_abort_batch(session, task, channel_nodes, tmp_path):
    """截断文件混在批次里：其余文件照常入库，files_bad 有一条，整批不抛异常。"""
    folder = _batch_dir(tmp_path)
    bad_name = f"{STAMP}-CH2_Task1.dat"
    _truncate(folder / bad_name)

    result = import_batch(session, task.id, folder, channel_nodes)

    # 整批不再抛异常，其余 3 个通道照常写入指标（3 通道 × 3 指标）
    assert result.files_ok == 3
    assert result.metrics_written == 9
    assert result.channels == 4
    # 损坏文件结构化记录：文件名 + 原因
    assert result.files_bad_count == 1
    entry = result.files_bad[0]
    assert isinstance(entry, BadFile)
    assert entry.filename == bad_name
    assert entry.channel_code == "CH2"
    assert "长度不一致" in entry.reason
    assert entry.damaged is True

    # 数据集保留，且备注能看出「有 1 个文件损坏」
    assert result.discarded is False
    assert result.dataset_id is not None
    row = datasets.get_dataset(session, result.dataset_id)
    # 三态（P1 / S5）：3 好 1 坏 = 部分有效，不再折叠成「无效」
    assert row.status == "部分有效"
    assert "损坏 1 个文件" in row.remark
    assert bad_name in row.remark

    # 损坏文件本身被标成无效，正常通道有值
    files = {f.channel_code: f for f in datasets.list_files(session, result.dataset_id)}
    assert files["CH2"].status == "无效"
    for code in ("CH1", "CH3", "CH4"):
        assert files[code].status == "有效"
        assert {v.metric_code for v in metric_store.list_values(session, task.id, code)} == {
            "v_RMS",
            "a_Peak",
            "a_RMS",
        }
    assert metric_store.list_values(session, task.id, "CH2") == []


def test_all_files_broken_leaves_no_dataset(session, task, channel_nodes, tmp_path):
    """整批文件全坏：不残留数据集记录（软删留痕），也不留指标值。"""
    folder = _batch_dir(tmp_path)
    for ch in CHANNELS:
        _truncate(folder / f"{STAMP}-{ch}_Task1.dat")

    before = datasets.list_datasets(session, task.id)
    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_ok == 0
    assert result.files_bad_count == 4
    assert result.discarded is True
    assert result.dataset_id is None
    assert datasets.list_datasets(session, task.id) == before  # 列表里没有空壳
    assert metric_store.list_values(session, task.id, "CH1") == []

    # 留痕在回收站（可追溯，不是静默丢弃）
    items = [item for item in recycle.list_recycle_items(session) if item.entity_type == "dataset"]
    assert len(items) == 1
    assert items[0].cascade["channel"] == 4


def test_all_files_judged_invalid_keeps_dataset(session, task, channel_nodes, tmp_path):
    """文件都读得出来、只是判无效（信号为零）：数据集照旧保留（改造前行为不变）。"""
    folder = tmp_path / "四通道保存" / STAMP
    for ch in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=np.zeros(int(FS * 2), dtype="float32"),
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.discarded is False
    assert result.dataset_id is not None
    assert result.files_ok == 0
    assert result.files_damaged == []  # 没有「损坏」文件，只是判无效
    assert result.files_bad_count == 4
    assert datasets.get_dataset(session, result.dataset_id).status == "无效"
    assert len(datasets.list_datasets(session, task.id)) == 1


def test_reimport_all_broken_keeps_existing_dataset(session, task, channel_nodes, tmp_path):
    """已导入过的批次再导（文件此刻全坏）：保留数据集与历史值，只标无效。"""
    folder = _batch_dir(tmp_path)
    good = import_batch(session, task.id, folder, channel_nodes)
    assert good.metrics_written == 12
    before_values = len(metric_store.list_values(session, task.id, "CH1"))

    for ch in CHANNELS:
        _truncate(folder / f"{STAMP}-{ch}_Task1.dat")
    again = import_batch(session, task.id, folder, channel_nodes)

    assert again.discarded is False
    assert again.dataset_id == good.dataset_id
    assert datasets.get_dataset(session, good.dataset_id).status == "无效"
    assert len(metric_store.list_values(session, task.id, "CH1")) == before_values


def test_all_good_batch_result_matches_previous_behaviour(session, task, channel_nodes, tmp_path):
    """全好批次：files_bad 为空，指标值与直接读文件计算逐位相同（回归）。"""
    folder = _batch_dir(tmp_path)

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_bad == []
    assert result.files_bad_count == 0
    assert result.discarded is False
    assert result.status == "有效"
    assert result.files_ok == 4
    assert result.channels == 4
    assert result.metrics_written == 12
    assert result.unmapped_channels == []
    assert datasets.get_dataset(session, result.dataset_id).remark == ""

    record = read_vibration(folder / f"{STAMP}-CH1_Task1.dat")
    # S4W-1：v_RMS 走逐窗路径并消费 window_fn（默认 hanning），时域指标仍整段；
    # 逐位对比因此用管线的同一入口 compute_metric_series，而不是整段单值接口
    expected = compute_metric_series(
        record.samples, record.meta.sample_rate_hz, codes=["v_RMS", "a_Peak", "a_RMS"]
    )
    stored = {v.metric_code: v for v in metric_store.list_values(session, task.id, "CH1")}
    for code, points in expected.items():
        assert len(points) == 1  # G12：2 s 记录 = 恰好一窗 → 1 个值
        assert stored[code].value == points[0][1]  # 逐位相同，不是近似
