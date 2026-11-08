"""节点健康状态：按最新指标值 vs 阈值判定，并沿子树汇总。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.health import node_health, point_level
from lion_monitor.features.metrics import store_metrics
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "h.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="任务H")


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


def _import_and_store(session, task, tmp_path, code: str, value: float, stamp: str):
    """造一个只含单通道的批次，直接写一个指标值。"""
    from lion_monitor.features.metrics import store_metrics

    folder = tmp_path / "批" / stamp
    write_dat(
        folder / f"{stamp}-CH{code[-1]}_Task1.dat",
        samples=np.zeros(int(FS), dtype="float32"),
        channel=code,
        started_at=stamp,
        sample_rate_hz=FS,
    )
    row = datasets.import_from_dir(session, task.id, folder)
    files = datasets.list_files(session, row.id)
    store_metrics(session, files[0].id, {"v_RMS": value, "a_Peak": value}, row.collected_at)
    return row


def test_point_level_respects_direction():
    assert point_level(1.0, (2.8, 4.5, ">", "通道级")) == "ok"
    assert point_level(3.0, (2.8, 4.5, ">", "通道级")) == "warn"
    assert point_level(9.0, (2.8, 4.5, ">", "通道级")) == "alert"
    assert point_level(1.0, (2.8, 4.5, "<", "通道级")) == "alert"
    assert point_level(1.0, None) == "unset"


def test_metric_node_state_uses_latest_value_and_threshold(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _import_and_store(session, task, tmp_path, "CH1", 1.0, "20261029150000")
    _import_and_store(session, task, tmp_path, "CH1", 3.2, "20261029160000")

    health = node_health(session, task.id)

    assert health[built["v_RMS"]].state == "warn"
    assert health[built["v_RMS"]].latest_value == pytest.approx(3.2)
    assert health[built["v_RMS"]].over_count == 1  # 只有 3.2 越过 warn


def test_state_rolls_up_to_ancestors(session, task, built, tmp_path):
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _import_and_store(session, task, tmp_path, "CH1", 9.9, "20261029150000")

    health = node_health(session, task.id)

    assert health[built["ch1"]].state == "alert"
    assert health[built["component"]].state == "alert"
    assert health[built["device"]].state == "alert"
    assert health[built["root"]].state == "alert"
    assert health[built["root"]].over_count == sum(h.over_count for h in [health[built["v_RMS"]]])


def test_nodes_without_values_or_thresholds_are_unset(session, task, built):
    health = node_health(session, task.id)

    assert health[built["v_RMS"]].state == "unset"
    assert health[built["v_RMS"]].latest_value is None
    assert health[built["ch2"]].state == "unset"


def _store_series(
    session,
    task,
    tmp_path,
    channel: str,
    code: str,
    series: list[tuple[str, float]],
    stale: bool = False,
):
    """按给定 (时刻, 值) 依次写一串点（每次一个批次）。"""
    for stamp, value in series:
        import numpy as np
        from _dat_fixture import write_dat

        folder = tmp_path / f"{code}_{stamp}"
        write_dat(
            folder / f"{stamp}-CH1_Task1.dat",
            samples=np.zeros(int(FS), dtype="float32"),
            channel="CH1",
            started_at=stamp,
            sample_rate_hz=FS,
        )
        row = datasets.import_from_dir(session, task.id, folder)
        files = datasets.list_files(session, row.id)
        store_metrics(session, files[0].id, {code: value}, row.collected_at, stale=stale)


def test_state_uses_worst_of_last_five_points(session, task, built, tmp_path):
    """2026-11-03：判级看最近 5 个点里最严重的一个，不再只看最新 1 点。

    最新点正常、但 5 点窗口内有一个越过 alert → 节点判 alert。
    """
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    series = [
        ("20261029150000", 1.0),
        ("20261029151000", 1.0),
        ("20261029152000", 9.9),  # 窗口内（倒数第 3 个）越过 alert
        ("20261029153000", 1.0),
        ("20261029154000", 1.0),  # 最新点正常
    ]
    _store_series(session, task, tmp_path, "CH1", "v_RMS", series)

    health = node_health(session, task.id)

    assert health[built["v_RMS"]].state == "alert"
    assert health[built["v_RMS"]].latest_value == pytest.approx(1.0)  # 显示仍是最新点


def test_points_older_than_window_do_not_grade(session, task, built, tmp_path):
    """窗口外（第 6 个及更早）的越限点不再影响状态。"""
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    series = [
        ("20261029140000", 9.9),  # 第 6 个：窗口外
        ("20261029150000", 1.0),
        ("20261029151000", 1.0),
        ("20261029152000", 1.0),
        ("20261029153000", 1.0),
        ("20261029154000", 1.0),
    ]
    _store_series(session, task, tmp_path, "CH1", "v_RMS", series)

    health = node_health(session, task.id)

    assert health[built["v_RMS"]].state == "ok"


def test_stale_points_do_not_grade(session, task, built, tmp_path):
    """待重算（旧参数算出）的点不参与判级：最新点是 stale 的越限值也不判 alert。"""
    limits.set_limit(session, task.id, built["ch1"], "v_RMS", 2.8, 4.5, ">")
    _import_and_store(session, task, tmp_path, "CH1", 1.0, "20261029150000")
    # 再写一条 stale 的越限点，让它成为最新点
    import numpy as np
    from _dat_fixture import write_dat

    folder = tmp_path / "stale"
    write_dat(
        folder / "20261029160000-CH1_Task1.dat",
        samples=np.zeros(int(FS), dtype="float32"),
        channel="CH1",
        started_at="20261029160000",
        sample_rate_hz=FS,
    )
    row = datasets.import_from_dir(session, task.id, folder)
    files = datasets.list_files(session, row.id)
    store_metrics(session, files[0].id, {"v_RMS": 9.9}, row.collected_at, stale=True)

    health = node_health(session, task.id)

    assert health[built["v_RMS"]].state == "ok", "stale 点不该把节点判成 alert"
