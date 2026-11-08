"""台账改名后健康树节点显示名同步（评审 P1-5 / 任务 D5）。"""

from __future__ import annotations

import pytest

from lion_monitor.store import channel_reg, metric_def, tasks, tree
from lion_monitor.store import object_registry as reg
from lion_monitor.store.db import init_db, make_engine, make_session_factory


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
def device(session, task):
    root = tree.root_node(session, task.id)
    return tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)


def test_rename_object_syncs_component_node_name(session, task, device):
    obj = reg.create_object(session, task.id, "隔膜过辊轴承", "轴承")
    node = tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, ref_id=obj.id
    )

    reg.update_object(session, obj.id, name="隔膜过辊轴承B")

    assert tree.get_node(session, node.id).name == "隔膜过辊轴承B"


def test_rename_object_leaves_manual_nodes_alone(session, task, device):
    obj = reg.create_object(session, task.id, "隔膜过辊轴承", "轴承")
    tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, ref_id=obj.id
    )
    manual = tree.create_node(session, task.id, "component", "手工命名对象", parent_id=device.id)

    reg.update_object(session, obj.id, name="隔膜过辊轴承B")

    assert tree.get_node(session, manual.id).name == "手工命名对象"


def test_rename_channel_syncs_channel_node_name(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    comp = tree.create_node(
        session, task.id, "component", "轴承A", parent_id=device.id, ref_id=obj.id
    )
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id)

    channel_reg.update_channel(session, ch.id, code="CH9")

    assert tree.get_node(session, node.id).name == "CH9"


def test_rename_metric_syncs_metric_node_name(session, task, device):
    obj = reg.create_object(session, task.id, "轴承A", "轴承")
    comp = tree.create_node(
        session, task.id, "component", "轴承A", parent_id=device.id, ref_id=obj.id
    )
    ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id)
    metric = metric_def.create_metric(session, task.id, "v_RMS", unit="mm/s")
    node = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch_node.id, ref_id=metric.id
    )

    metric_def.update_metric(session, metric.id, code="v_RMS_新")

    assert tree.get_node(session, node.id).name == "v_RMS_新"


def test_rename_metric_rejects_duplicate_code(session, task):
    metric_def.create_metric(session, task.id, "v_RMS")
    other = metric_def.create_metric(session, task.id, "a_Peak")

    with pytest.raises(metric_def.MetricDefError, match="已存在"):
        metric_def.update_metric(session, other.id, code="v_RMS")
