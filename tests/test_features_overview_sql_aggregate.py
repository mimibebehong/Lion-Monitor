"""工作台概览改走 SQL 聚合（P3 / S2）：超限记录只从库里取越限行、健康分布与九步进度不再全量拉值。

口径不变（方案 9.10 + §14.1 Q1–Q6 + P2 / S1）：

- 超限记录：每个「通道节点 × 指标 × 计算点」一条，同一通道节点下多个指标节点共读一份值时只算一次
- 健康分布 / 设备总览：指标节点按最新一点判级（``features.health``）
- 九步：⑨ 趋势 = 至少一条序列有两个以上计算点
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from _sql_count import counted_statements

from lion_monitor.features import overview, series
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store.models import MetricValue

START = "2026-11-01 10:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "o.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def built(session, tmp_path):
    """设备 → 对象 → CH1 通道节点（v_RMS + a_Peak）+ CH2 通道节点（v_RMS）。"""
    task = tasks.create_task(session, name="概览任务")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    vrms = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch1.id, attrs={"code": "v_RMS"}
    )
    apk = tree.create_node(
        session, task.id, "metric", "a_Peak", parent_id=ch1.id, attrs={"code": "a_Peak"}
    )
    ch2 = tree.create_node(session, task.id, "channel", "CH2", parent_id=comp.id)
    vrms2 = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch2.id, attrs={"code": "v_RMS"}
    )
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    file1 = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)
    file2 = datasets.add_file(session, ds.id, folder / "CH2_Task1.dat", "CH2", 32000.0, 64000)
    return {
        "task": task,
        "device": device.id,
        "comp": comp.id,
        "ch1": ch1.id,
        "vrms": vrms.id,
        "apk": apk.id,
        "ch2": ch2.id,
        "vrms2": vrms2.id,
        "file1": file1.id,
        "file2": file2.id,
    }


def _add_points(session, file_id, node_id, code, values, *, start=START):
    base = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    for index, value in enumerate(values):
        session.add(
            MetricValue(
                channel_id=file_id,
                node_id=node_id,
                code=code,
                value=value,
                unit="mm/s",
                level="unset",
                computed_at=base + timedelta(seconds=index),
            )
        )
    session.commit()


def test_over_records_only_takes_over_limit_rows_from_sql(session, built):
    _add_points(session, built["file1"], built["ch1"], "v_RMS", [1.0, 3.2, 1.2, 5.0, 0.9])
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)

    with counted_statements(session.get_bind()) as seen:
        records = overview.over_records(session, built["task"].id)

    assert [record.value for record in records] == [pytest.approx(3.2), pytest.approx(5.0)]
    assert [record.level for record in records] == ["warn", "alert"]
    assert any("value" in text.lower() for text in seen)


def test_over_records_needs_no_full_series_pull(session, built, monkeypatch):
    """超限记录不再走 ``metric_series``（那会把每条序列的全部点拉成 ValueRow）。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("over_records 不该再走 metric_series 全量拉取")

    monkeypatch.setattr(series, "metric_series", _boom)
    _add_points(session, built["file1"], built["ch1"], "v_RMS", [1.0, 3.2])
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)

    records = overview.over_records(session, built["task"].id)

    assert len(records) == 1
    assert records[0].node_id == built["ch1"]


def test_over_records_counts_each_channel_node_once(session, built, tmp_path):
    """同一通道节点下两个指标节点共读一份值：超限记录只算一次（不是两条）。"""
    twin = tree.create_node(
        session,
        built["task"].id,
        "metric",
        "v_RMS",
        parent_id=built["ch1"],
        attrs={"code": "v_RMS"},
    )
    assert twin.id != built["vrms"]
    _add_points(session, built["file1"], built["ch1"], "v_RMS", [3.3])
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)
    limits.set_limit(session, built["task"].id, twin.id, "v_RMS", 2.8, 4.5)

    records = overview.over_records(session, built["task"].id)

    assert len(records) == 1


def test_task_overview_works_without_metric_series(session, built, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("task_overview 不该再走 metric_series 全量拉取")

    monkeypatch.setattr(series, "metric_series", _boom)
    _add_points(session, built["file1"], built["ch1"], "v_RMS", [3.2, 9.9])
    _add_points(session, built["file2"], built["ch2"], "v_RMS", [0.5])
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)

    summary = overview.task_overview(session, built["task"].id)

    assert summary.metric_value_count == 3
    assert summary.over_count == 2
    assert summary.over_level_count("alert") == 1
    assert summary.health_counts["alert"] == 1
    assert summary.devices[0].over_metric_count == 1


def test_task_overview_statement_count_is_bounded(session, built):
    for node, file_id, code in (
        (built["ch1"], built["file1"], "v_RMS"),
        (built["ch1"], built["file1"], "a_Peak"),
        (built["ch2"], built["file2"], "v_RMS"),
    ):
        _add_points(session, file_id, node, code, [1.0] * 200)
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)

    with counted_statements(session.get_bind()) as seen:
        overview.task_overview(session, built["task"].id)

    assert len(seen) <= 60, f"语句数 {len(seen)} 条，疑似按点/按节点查询"


def test_task_overview_stays_fast_on_6000_points(session, built):
    """6000 点（原口径要逐点判级）必须是一次性聚合：给 1 秒的宽松上限。"""
    for node, file_id, code in (
        (built["ch1"], built["file1"], "v_RMS"),
        (built["ch1"], built["file1"], "a_Peak"),
        (built["ch2"], built["file2"], "v_RMS"),
    ):
        _add_points(session, file_id, node, code, [2.0] * 2000)
    limits.set_limit(session, built["task"].id, built["vrms"], "v_RMS", 2.8, 4.5)

    started = time.perf_counter()
    summary = overview.task_overview(session, built["task"].id)
    elapsed = time.perf_counter() - started

    assert summary.metric_value_count == 6000
    assert elapsed < 1.0, f"6000 点工作台耗时 {elapsed * 1000:.0f} ms"


def test_step_states_trend_step_uses_point_counts(session, built):
    _add_points(session, built["file1"], built["ch1"], "v_RMS", [1.0])
    steps = {code: state for code, _name, state in overview.step_states(session, built["task"].id)}
    assert steps["⑨"] != "done"  # 只有一个点

    _add_points(session, built["file1"], built["ch1"], "v_RMS", [1.1], start="2026-11-01 11:00:00")
    steps = {code: state for code, _name, state in overview.step_states(session, built["task"].id)}
    assert steps["⑨"] == "done"
