"""导入管线的指标计算开关与「以健康树配置为准」测试。"""

from __future__ import annotations

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.metrics import compute_metrics
from lion_monitor.features.pipeline import aggregate_plan, import_batch, metric_plan
from lion_monitor.store import metrics as metric_store
from lion_monitor.store import tasks, tree
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


@pytest.fixture()
def app_session_pair(session):
    return session, tasks.create_task(session, name="任务B")


def _channel_tree(session, task, channel_metrics: dict[str, list[str]]) -> dict[str, int]:
    """按 {通道号: [指标码...]} 装配健康树，返回 通道号 → 通道节点 id。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes: dict[str, int] = {}
    for code, metrics in channel_metrics.items():
        channel = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
        nodes[code] = channel.id
        for metric_code in metrics:
            tree.create_node(
                session,
                task.id,
                "metric",
                metric_code,
                parent_id=channel.id,
                attrs={"code": metric_code},
            )
    return nodes


def _batch_dir(tmp_path, channels=(1, 2)):
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * 2)) / FS
    samples = np.sin(2 * np.pi * 100 * t).astype("float32")
    for ch in channels:
        write_dat(
            folder / f"{STAMP}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def test_compute_metrics_can_limit_to_codes():
    t = np.arange(int(FS * 2)) / FS
    values = compute_metrics(np.sin(2 * np.pi * 100 * t), FS, codes=["v_RMS"])
    assert set(values) == {"v_RMS"}


def test_import_without_compute_writes_no_metric_values(session, task, tmp_path):
    nodes = _channel_tree(session, task, {"CH1": ["v_RMS"], "CH2": ["v_RMS"]})

    result = import_batch(session, task.id, _batch_dir(tmp_path), nodes, compute=False)

    assert result.metrics_written == 0
    assert result.status == "有效"  # 结构校验仍通过
    assert metric_store.list_values(session, task.id, "CH1") == []


def test_import_computes_only_metrics_configured_in_tree(session, task, tmp_path):
    nodes = _channel_tree(
        session,
        task,
        {"CH1": ["v_RMS", "a_Peak"], "CH2": ["a_RMS"]},
    )

    result = import_batch(session, task.id, _batch_dir(tmp_path), nodes)

    assert result.metrics_written == 3
    ch1_codes = {v.metric_code for v in metric_store.list_values(session, task.id, "CH1")}
    ch2_codes = {v.metric_code for v in metric_store.list_values(session, task.id, "CH2")}
    assert ch1_codes == {"v_RMS", "a_Peak"}
    assert ch2_codes == {"a_RMS"}


def test_channel_without_metric_nodes_is_reported(session, task, tmp_path):
    nodes = _channel_tree(session, task, {"CH1": ["v_RMS"], "CH2": []})

    result = import_batch(session, task.id, _batch_dir(tmp_path), nodes)

    assert result.metrics_written == 1
    assert result.channels_without_metrics == ["CH2"]


def test_result_exposes_planned_metrics(session, task, tmp_path):
    nodes = _channel_tree(session, task, {"CH1": ["v_RMS"], "CH2": ["a_Peak"]})

    result = import_batch(session, task.id, _batch_dir(tmp_path), nodes, compute=False)

    assert result.planned_metrics == {"CH1": ["v_RMS"], "CH2": ["a_Peak"]}


def test_same_channel_code_under_two_objects(app_session_pair):
    """同一个通道号挂在两个监测对象下：计划表要出两行，计算取并集。"""
    session, task = app_session_pair
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)

    bearing = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    motor = tree.create_node(session, task.id, "component", "入片牵引电机", parent_id=device.id)

    ch_a = tree.create_node(session, task.id, "channel", "CH2", parent_id=bearing.id)
    tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch_a.id, attrs={"code": "v_RMS"}
    )
    ch_b = tree.create_node(session, task.id, "channel", "CH2", parent_id=motor.id)
    for code in ("v_RMS", "a_Peak"):
        tree.create_node(session, task.id, "metric", code, parent_id=ch_b.id, attrs={"code": code})

    rows = metric_plan(session, task.id)

    assert [(r.object_name, r.channel_code, r.metric_codes) for r in rows] == [
        ("隔膜过辊轴承", "CH2", ["v_RMS"]),
        ("入片牵引电机", "CH2", ["v_RMS", "a_Peak"]),
    ]
    assert aggregate_plan(rows) == {"CH2": ["v_RMS", "a_Peak"]}


def test_import_uses_union_for_duplicated_channel_code(app_session_pair, tmp_path):
    session, task = app_session_pair
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp_a = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    comp_b = tree.create_node(session, task.id, "component", "电机B", parent_id=device.id)
    for comp, codes in ((comp_a, ["v_RMS"]), (comp_b, ["a_Peak"])):
        ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        for code in codes:
            tree.create_node(
                session, task.id, "metric", code, parent_id=ch.id, attrs={"code": code}
            )

    nodes = {r.channel_code: 0 for r in metric_plan(session, task.id)}
    result = import_batch(
        session, task.id, _batch_dir(tmp_path, channels=(1,)), nodes or {"CH1": 0}
    )

    ch1_codes = {v.metric_code for v in metric_store.list_values(session, task.id, "CH1")}
    assert ch1_codes == {"v_RMS", "a_Peak"}
    assert result.metrics_written == 2
