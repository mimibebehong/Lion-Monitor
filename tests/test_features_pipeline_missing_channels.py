"""G2 回归测试：``ImportResult.missing_channels`` = 健康树已装配 − 本批实际有文件的通道。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G2）：
树上装配了 4 个通道、本批只导入 3 个文件时，批次状态仍判「有效」、页面无任何提示，
现场看不出「漏采」。口径（P4-2 任务书）：

- 只提示，不阻断导入，也不改批次状态三态（P1 已落地）
- 与 ``files_bad`` 语义分开：``files_bad`` 是「文件进来但坏了」，本字段是「根本没文件」
- 树上没有的通道不算缺失（3 通道树 + 3 文件 = 空列表）
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261029152834"
ALL_CHANNELS = ("CH1", "CH2", "CH3", "CH4")
METRICS = ("v_RMS", "a_Peak", "a_RMS")


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务缺通道")


def _tree(session, task, channels) -> dict[str, int]:
    """健康树：设备 → 监测对象 → 每个通道节点下挂三个指标节点。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes: dict[str, int] = {}
    for code in channels:
        node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
        nodes[code] = node.id
        for metric_code in METRICS:
            tree.create_node(
                session,
                task.id,
                "metric",
                metric_code,
                parent_id=node.id,
                attrs={"code": metric_code},
            )
    return nodes


def _batch(tmp_path, channels) -> Path:
    """造一个批次目录：每通道 2 s / 32 kHz 正弦，幅值 1（G12 后要够一窗才出值）。"""
    folder = tmp_path / "批次保存" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = np.sin(2 * np.pi * 100 * t).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=samples,
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def test_tree_four_channels_batch_three_files_reports_ch4(session, task, tmp_path):
    """4 通道树 + 只导 3 个文件 → missing_channels == ["CH4"]，状态与导入不阻断。"""
    nodes = _tree(session, task, ALL_CHANNELS)
    folder = _batch(tmp_path, ("CH1", "CH2", "CH3"))

    result = import_batch(session, task.id, folder, nodes)

    assert result.missing_channels == ["CH4"]
    # 只提示：该进的照进，批次状态仍是「有效」，损坏文件列表不受影响
    assert result.dataset_id is not None
    assert datasets.get_dataset(session, result.dataset_id).status == "有效"
    assert result.files_bad == []
    assert result.files_ok == 3
    assert result.metrics_written == 9


def test_tree_four_channels_batch_four_files_no_missing(session, task, tmp_path):
    """4 通道树 + 4 个文件 → 空列表。"""
    nodes = _tree(session, task, ALL_CHANNELS)
    folder = _batch(tmp_path, ALL_CHANNELS)

    result = import_batch(session, task.id, folder, nodes)

    assert result.missing_channels == []


def test_tree_subset_does_not_invent_missing_channels(session, task, tmp_path):
    """3 通道树 + 3 个文件 → 空列表（树上没有的通道不算缺失）。"""
    nodes = _tree(session, task, ("CH1", "CH2", "CH3"))
    folder = _batch(tmp_path, ("CH1", "CH2", "CH3"))

    result = import_batch(session, task.id, folder, nodes)

    assert result.missing_channels == []


def test_missing_channels_keeps_tree_order(session, task, tmp_path):
    """多个通道没文件时按健康树顺序给出，方便页面直接拼接展示。"""
    nodes = _tree(session, task, ALL_CHANNELS)
    folder = _batch(tmp_path, ("CH2",))

    result = import_batch(session, task.id, folder, nodes)

    assert result.missing_channels == ["CH1", "CH3", "CH4"]
