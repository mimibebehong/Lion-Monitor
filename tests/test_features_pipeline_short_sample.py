"""G12：不足一窗的样本在导入管线里直接判无效（不出值 + 原因进结果 / 日志）。

马哥 2026-11-02 定：「不足指标计算窗口的数据直接判无效」。
S4W-1 / S4W-2 时整段短于一窗会被当成 1 个窗算出 0 值（1~63 点这种无效样本也出值），
本卡撤销该兜底：``n < window_s × fs`` → 该指标不出值，``ImportResult.skipped_short``
带具体原因（如 ``样本 32 点，不足一窗（2 s @ 32000 Hz 需 64000 点）``），文件判无效，
日志文件留 warning 行。恰好一窗（64000 点）仍然有效。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import ShortSample, import_batch
from lion_monitor.logging_setup import setup_logging
from lion_monitor.store import datasets, metric_assign, tasks, tree
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261029152834"
CHANNEL = "CH1"


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "g12.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务G12")


@pytest.fixture()
def channel_nodes(session, task):
    """健康树：监测对象下 1 个通道节点，挂 v_RMS / a_Peak / a_RMS 三个指标。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    node = tree.create_node(session, task.id, "channel", CHANNEL, parent_id=comp.id)
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        tree.create_node(session, task.id, "metric", code, parent_id=node.id, attrs={"code": code})
    return {CHANNEL: node.id}


def _batch_dir(tmp_path: Path, points: int, channel: str = CHANNEL) -> Path:
    """造一个批次目录：单通道 ``points`` 点 / 32 kHz / 100 Hz 正弦（幅值 1 g）。"""
    folder = tmp_path / "单通道保存" / STAMP
    t = np.arange(points) / FS
    samples = np.sin(2 * np.pi * 100 * t).astype("float32")
    write_dat(
        folder / f"{STAMP}-{channel}_Task1.dat",
        samples=samples,
        channel=channel,
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def test_32_point_file_is_invalid_with_short_sample_reason(session, task, channel_nodes, tmp_path):
    """验收 1：32 点样本 → 0 个值，原因含「32 点」「不足一窗」「64000」。"""
    folder = _batch_dir(tmp_path, 32)

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.metrics_written == 0
    assert result.files_ok == 0
    assert result.status == "无效"
    assert len(result.files_bad) == 1
    entry = result.files_bad[0]
    assert entry.damaged is False
    for token in ("32 点", "不足一窗", "64000"):
        assert token in entry.reason, entry.reason
    # 明细：每个被跳过的指标各一条，带节点与指标码
    assert result.skipped_short is not None
    assert {item.code for item in result.skipped_short} == {"v_RMS", "a_Peak", "a_RMS"}
    assert all(isinstance(item, ShortSample) for item in result.skipped_short)
    assert all("不足一窗" in item.reason for item in result.skipped_short)
    # 值一个都不落库
    assert metric_store.list_values(session, task.id, CHANNEL) == []
    row = datasets.get_dataset(session, result.dataset_id)
    assert row.status == "无效"
    assert "不足一窗" in row.remark


def test_one_second_file_is_invalid(session, task, channel_nodes, tmp_path):
    """验收 2：32000 点（1 s，不足 2 s 窗）→ 0 个值。"""
    folder = _batch_dir(tmp_path, int(FS))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.metrics_written == 0
    assert result.files_ok == 0
    assert result.status == "无效"
    assert "32000 点" in result.files_bad[0].reason
    assert metric_store.list_values(session, task.id, CHANNEL) == []


def test_exactly_one_window_writes_one_point_per_metric(session, task, channel_nodes, tmp_path):
    """验收 3：64000 点（恰好 2 s 窗）→ 每个指标 1 个值，批次有效。"""
    folder = _batch_dir(tmp_path, int(FS * 2))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.files_ok == 1
    assert result.status == "有效"
    assert result.metrics_written == 3
    assert result.files_bad == []
    assert not (result.skipped_short or [])
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        assert len(metric_store.list_values(session, task.id, CHANNEL, code)) == 1, code


def test_four_seconds_writes_two_points_per_metric(session, task, channel_nodes, tmp_path):
    """验收 4：128000 点（4 s，2 s 窗 2 s 步）→ 每个指标 2 个值。"""
    folder = _batch_dir(tmp_path, int(FS * 4))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.status == "有效"
    assert result.metrics_written == 6
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        assert len(metric_store.list_values(session, task.id, CHANNEL, code)) == 2, code


def test_node_window_s_decides_short_sample_per_metric(session, task, channel_nodes, tmp_path):
    """生效窗长按节点解析：v_RMS 被授 1 s 窗 → 1 s 文件照样出 1 个值，
    另两个指标仍按默认 2 s 窗判短（同一个文件，各指标各判各的）。"""
    metric_assign.assign_metric(
        session, task.id, channel_nodes[CHANNEL], "v_RMS", params={"window_s": 1.0}
    )
    session.commit()
    folder = _batch_dir(tmp_path, int(FS))

    result = import_batch(session, task.id, folder, channel_nodes)

    assert result.metrics_written == 1
    assert len(metric_store.list_values(session, task.id, CHANNEL, "v_RMS")) == 1
    assert {item.code for item in (result.skipped_short or [])} == {"a_Peak", "a_RMS"}
    assert all(
        "不足一窗（2 s @ 32000 Hz 需 64000 点）" in item.reason
        for item in result.skipped_short or []
    )


def test_short_sample_reason_lands_in_log_file(session, task, channel_nodes, tmp_path):
    """验收 5（日志侧）：logs/lion-monitor.log 里能看到具体跳过原因。"""
    log_file = setup_logging(log_dir=tmp_path / "logs")
    try:
        folder = _batch_dir(tmp_path, 32)

        import_batch(session, task.id, folder, channel_nodes)

        text = log_file.read_text(encoding="utf-8")
        assert "不足一窗" in text, text
        assert "32 点" in text, text
        assert f"{STAMP}-{CHANNEL}_Task1.dat" in text, text
    finally:
        import logging

        root = logging.getLogger()
        for handler in [h for h in root.handlers if getattr(h, "_lion_logging_file", False)]:
            root.removeHandler(handler)
            handler.close()
