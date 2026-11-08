"""S4W-2 管线验收：时域指标（a_Peak / a_RMS）一次导入落多个时点。

口径与 S4W-1 一致：``computed_at = 数据集采集起始时间 + 窗中心偏移``
（20 s / 2 s 窗 → +1,3,…,19 s）；同一批里频域与时域指标点数相同，
``metrics_written`` = 实际行数。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features import trend_view
from lion_monitor.features.metrics import compute_metric_series
from lion_monitor.features.pipeline import import_batch
from lion_monitor.params import default_params
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
    return tasks.create_task(session, name="任务S4W2")


def _channel(session, task, codes=("a_Peak", "a_RMS")) -> int:
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for code in codes:
        tree.create_node(session, task.id, "metric", code, parent_id=ch1.id, attrs={"code": code})
    return ch1.id


def _samples(seconds: float = 20.0) -> np.ndarray:
    t = np.arange(int(FS * seconds)) / FS
    return (0.8 * np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 300 * t)).astype("float32")


def _batch_dir(tmp_path, seconds: float = 20.0) -> Path:
    folder = tmp_path / "批次" / STAMP
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=_samples(seconds),
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def test_import_20s_writes_ten_points_per_time_domain_metric(session, task, tmp_path):
    """验收 1（落库侧）：20 s / 2 s 窗 → a_Peak、a_RMS 各 10 行，computed_at 为窗中心。"""
    ch1 = _channel(session, task)
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.status == "有效"
    assert result.metrics_written == 20  # 2 指标 × 10 窗
    for code in ("a_Peak", "a_RMS"):
        rows = metric_store.list_values(session, task.id, "CH1", code, node_id=ch1)
        assert len(rows) == 10, code
        assert [row.computed_at for row in rows] == [
            f"2026-10-29 15:28:{second:02d}" for second in range(35, 54, 2)
        ], code
        assert all(row.value > 0 for row in rows), code


def test_pipeline_values_match_compute_metric_series(session, task, tmp_path):
    """落库值与逐窗接口一致（管线不再自己算一份时域统计）。"""
    ch1 = _channel(session, task)
    import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    expected = compute_metric_series(
        _samples(20.0).astype("float64"),
        FS,
        params=default_params("a_RMS"),
        codes=["a_Peak", "a_RMS"],
    )
    for code in ("a_Peak", "a_RMS"):
        rows = metric_store.list_values(session, task.id, "CH1", code, node_id=ch1)
        assert [row.value for row in rows] == pytest.approx(
            [value for _offset, value in expected[code]], rel=1e-6
        ), code


def test_frequency_and_time_metrics_share_the_same_point_count(session, task, tmp_path):
    """同一批里 v_RMS 与 a_Peak 点数一致（一套窗参数 → 一套时点）。"""
    ch1 = _channel(session, task, codes=("v_RMS", "a_Peak"))
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.metrics_written == 20
    counts = {
        code: len(metric_store.list_values(session, task.id, "CH1", code, node_id=ch1))
        for code in ("v_RMS", "a_Peak")
    }
    assert counts == {"v_RMS": 10, "a_Peak": 10}


def test_time_domain_window_params_come_from_node_override(session, task, tmp_path):
    """节点覆盖 window_s=5 / step_s=2 → 8 点（末段不足一窗丢弃），默认仍是 2 s / 2 s。"""
    ch1 = _channel(session, task, codes=("a_RMS",))
    metric_assign.assign_metric(
        session, task.id, ch1, "a_RMS", params={"window_s": 5.0, "step_s": 2.0}
    )
    result = import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    assert result.metrics_written == 8
    rows = metric_store.list_values(session, task.id, "CH1", "a_RMS", node_id=ch1)
    assert [row.computed_at for row in rows] == [
        f"2026-10-29 15:28:{second:02d}.500000" for second in (36, 38, 40, 42, 44, 46, 48, 50)
    ]


def test_trend_reads_ten_time_domain_points(session, task, tmp_path):
    """趋势取数（页面同一入口）对 a_Peak 也是 10 个点、按时间升序、不抽稀。"""
    ch1 = _channel(session, task, codes=("a_Peak",))
    import_batch(session, task.id, _batch_dir(tmp_path), {"CH1": ch1})

    rows = metric_store.list_values(session, task.id, "CH1", "a_Peak", node_id=ch1)
    plotted, decimated = trend_view.chart_points(rows)
    assert len(rows) == len(plotted) == 10
    assert decimated is False
    assert [row.computed_at for row in plotted] == sorted(row.computed_at for row in plotted)
