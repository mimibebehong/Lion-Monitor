"""工作台概览：任务/数据集/超限记录/健康分布/流程进度全部来自数据库。"""

from __future__ import annotations

import pytest

from lion_monitor.features import overview
from lion_monitor.features.metrics import store_metrics
from lion_monitor.store import datasets, limits, metric_def, sensors, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "o.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务O")


@pytest.fixture()
def built(session, task):
    """任务 → 设备 → 对象 → CH1(v_RMS, a_Peak) / CH2(v_RMS)。"""
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
    return {
        "root": root.id,
        "device": device.id,
        "component": comp.id,
        "ch1": ch1.id,
        "v_RMS": vrms.id,
        "a_Peak": apk.id,
        "ch2": ch2.id,
        "v_RMS2": vrms2.id,
    }


def _store(session, tmp_path, task, code: str, points: list[tuple[str, float]]) -> None:
    """造一个单通道批次，按时间点写入 v_RMS 值。"""
    stamp = points[0][0]
    folder = tmp_path / stamp
    row = datasets.create_dataset(session, task.id, stamp, str(folder), stamp)
    file = datasets.add_file(session, row.id, folder / f"{code}.dat", code, FS, 1000)
    for when, value in points:
        store_metrics(session, file.id, {"v_RMS": value}, when)


# ---------------------------------------------------------------- 超限记录


def test_over_records_lists_every_point_over_warn(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 1.0)])
    _store(
        session,
        tmp_path,
        task,
        "CH1",
        [("2026-10-29 16:00:00", 3.2), ("2026-10-29 17:00:00", 9.9)],
    )

    records = overview.over_records(session, task.id)

    assert [r.level for r in records] == ["warn", "alert"]
    assert [r.value for r in records] == pytest.approx([3.2, 9.9])
    first = records[0]
    assert (first.channel, first.code, first.unit) == ("CH1", "v_RMS", "mm/s")
    assert first.computed_at == "2026-10-29 16:00:00"
    assert (first.warn, first.alert, first.source) == (2.8, 4.5, "通道级")


def test_over_records_empty_without_limits(session, task, built, tmp_path):
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 9.9)])

    assert overview.over_records(session, task.id) == []


# ---------------------------------------------------------------- 健康分布


def test_health_counts_counts_metric_nodes_by_state(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 3.2)])

    counts = overview.health_counts(session, task.id)

    assert counts == {"ok": 0, "warn": 1, "alert": 0, "invalid": 0, "unset": 2}


# ---------------------------------------------------------------- 设备总览


def test_device_overview_aggregates_subtree(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 3.2)])

    devices = overview.device_overviews(session, task.id)

    assert len(devices) == 1
    device = devices[0]
    assert device.name == "卷绕产线"
    assert device.state == "warn"
    assert (device.object_count, device.channel_count, device.metric_count) == (1, 2, 3)
    assert device.over_metric_count == 1


# ---------------------------------------------------------------- 流程进度


def test_step_states_on_fresh_task(session, task):
    steps = overview.step_states(session, task.id)

    assert [code for code, _name, _state in steps] == [c for c, _n in overview.STEPS]
    assert [state for _c, _n, state in steps][:3] == ["done", "current", "todo"]


def test_step_states_all_done_when_everything_configured(session, task, built, tmp_path):
    sensors.create_sensor(session, task.id, built["ch1"], model="IEPE 500mV/g")
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        metric_def.create_metric(session, task.id, code)
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _store(
        session,
        tmp_path,
        task,
        "CH1",
        [("2026-10-29 15:00:00", 1.0), ("2026-10-29 16:00:00", 1.1)],
    )
    for row in datasets.list_datasets(session, task.id):
        datasets.update_dataset(session, row.id, status="有效")

    states = [state for _c, _n, state in overview.step_states(session, task.id)]

    assert states == ["done"] * 9


def test_step_validity_stays_current_while_pending(session, task, built, tmp_path):
    sensors.create_sensor(session, task.id, built["ch1"], model="IEPE 500mV/g")
    for code in ("v_RMS", "a_Peak", "a_RMS"):
        metric_def.create_metric(session, task.id, code)
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 1.0)])

    states = [state for _c, _n, state in overview.step_states(session, task.id)]

    assert states[:6] == ["done"] * 5 + ["current"]  # ⑥ 有效性：还有待判数据集


# ---------------------------------------------------------------- 汇总


def test_task_overview_pulls_every_number_from_db(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _store(
        session,
        tmp_path,
        task,
        "CH1",
        [("2026-10-29 15:00:00", 3.2), ("2026-10-29 16:00:00", 9.9)],
    )
    _store(session, tmp_path, task, "CH2", [("2026-10-29 17:00:00", 0.5)])

    summary = overview.task_overview(session, task.id)

    assert summary.task_count == 1
    assert summary.dataset_count == 2
    assert summary.channel_count == 2
    assert summary.metric_value_count == 3
    assert len(summary.over_records) == 2
    assert summary.health_counts == {"ok": 0, "warn": 0, "alert": 1, "invalid": 0, "unset": 2}
    assert len(summary.devices) == 1
    assert summary.pending_validity == 2


def test_task_overview_works_on_empty_db(session):
    summary = overview.task_overview(session, None)

    assert summary.task_count == 0
    assert summary.dataset_count == 0
    assert summary.channel_count == 0
    assert summary.metric_value_count == 0
    assert summary.over_records == []
    assert summary.devices == []
    assert summary.health_counts == {"ok": 0, "warn": 0, "alert": 0, "invalid": 0, "unset": 0}
    assert {state for _c, _n, state in summary.steps} == {"todo"}


# ---------------------------------------------------------------- 最近批次


def test_recent_datasets_newest_first(session, task, built, tmp_path):
    _store(session, tmp_path, task, "CH1", [("2026-10-29 15:00:00", 1.0)])
    _store(session, tmp_path, task, "CH1", [("2026-10-29 17:00:00", 1.0)])

    recent = overview.recent_datasets(session, task.id)

    assert [row.collected_at for row in recent] == [
        "2026-10-29 17:00:00",
        "2026-10-29 15:00:00",
    ]
