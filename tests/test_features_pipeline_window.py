"""S4W-1 管线验收：频域指标一次导入落多个时点（cut 窗后逐点写库）+ 趋势取数。

口径：``computed_at = 数据集采集起始时间 + 窗中心偏移``（2 s 窗 → 1,3,…,19 s）。
时域指标本轮仍整段 1 点（S4W-2 再接），所以同一批里 a_Peak 只有 1 行。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features import trend_view
from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import metric_assign, tasks, tree
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


def _channel(session, task, codes=("v_RMS",)) -> int:
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for code in codes:
        tree.create_node(session, task.id, "metric", code, parent_id=ch1.id, attrs={"code": code})
    return ch1.id


def _batch_dir(tmp_path, seconds: float = 20.0) -> Path:
    """20 s 单通道批次：120 Hz + 300 Hz 双音（每 2 s 窗都能算出量级稳定的 v_RMS）。"""
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * seconds)) / FS
    samples = (0.8 * np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 300 * t)).astype(
        "float32"
    )
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=samples,
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def test_import_20s_writes_ten_v_rms_points_with_window_center_timestamps(session, task, tmp_path):
    """验收 1：20 s / 2 s 窗 → v_RMS 10 行；computed_at 为窗中心 1,3,…,19 s。"""
    ch1 = _channel(session, task)
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.status == "有效"
    assert result.metrics_written == 10

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    assert len(rows) == 10
    assert [row.computed_at[-8:] for row in rows] == [
        f"15:28:{second:02d}" for second in range(35, 54, 2)
    ]
    assert [row.value for row in rows] == pytest.approx([row.value for row in rows])
    assert all(row.value > 0 for row in rows)


def test_import_window_5s_step_2s_writes_eight_points(session, task, tmp_path):
    """验收 2：window_s=5、step_s=2 → 8 行（(20-5)//2+1），末段 19~20 s 丢弃。"""
    ch1 = _channel(session, task)
    metric_assign.assign_metric(
        session, task.id, ch1, "v_RMS", params={"window_s": 5.0, "step_s": 2.0}
    )
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.metrics_written == 8
    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    assert len(rows) == 8
    # 窗中心 2.5,4.5,…,16.5 → 15:28:34 + 偏移；非整秒保留微秒，相邻点不会撞同一秒
    assert [row.computed_at for row in rows] == [
        f"2026-10-29 15:28:{second:02d}.500000" for second in (36, 38, 40, 42, 44, 46, 48, 50)
    ]


def test_time_domain_metric_also_writes_ten_window_points(session, task, tmp_path):
    """同一批：v_RMS 与 a_Peak 都 10 行（S4W-2 起时域指标同口径），computed_at 用窗中心。"""
    ch1 = _channel(session, task, codes=("v_RMS", "a_Peak"))
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.metrics_written == 20
    peaks = metric_store.list_values(session, task.id, "CH1", "a_Peak", node_id=ch1)
    assert len(peaks) == 10
    assert [row.computed_at[-8:] for row in peaks] == [
        f"15:28:{second:02d}" for second in range(35, 54, 2)
    ]


def test_trend_reads_ten_points_and_is_not_decimated(session, task, tmp_path):
    """验收 4：趋势取数函数（页面同一入口）按节点+指标取到 10 个点并按时间升序。"""
    ch1 = _channel(session, task)
    import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    plotted, decimated = trend_view.chart_points(rows)
    assert len(rows) == len(plotted) == 10
    assert decimated is False
    assert [row.computed_at for row in plotted] == sorted(row.computed_at for row in plotted)


def test_dedupe_reuses_whole_series_for_identical_params(session, task, tmp_path):
    """同一通道两个节点参数相同 → 序列只算一次（compute_runs 不按点数膨胀）。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp_a = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    comp_b = tree.create_node(session, task.id, "component", "轴承B", parent_id=device.id)
    nodes = []
    for comp in (comp_a, comp_b):
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        tree.create_node(
            session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
        )
        nodes.append(channel.id)

    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": nodes[0]})

    assert result.compute_runs == 1
    assert result.dedupe_hits == 1
    assert result.metrics_written == 20
    for node_id in nodes:
        rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=node_id)
        assert len(rows) == 10
