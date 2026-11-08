"""通道台账（channel_reg）与指标台账（metric_def）测试。"""

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
def bearing(session, task):
    return reg.create_object(session, task.id, "隔膜过辊轴承", "轴承")


# ---------------------------------------------------------------- 通道台账


def test_create_channel_under_object(session, task, bearing):
    row = channel_reg.create_channel(session, task.id, bearing.id, "CH1")

    assert row.code == "CH1"
    assert row.object_name == "隔膜过辊轴承"
    assert [r.code for r in channel_reg.list_channels(session, task.id)] == ["CH1"]


def test_create_channel_rejects_duplicate_code(session, task, bearing):
    channel_reg.create_channel(session, task.id, bearing.id, "CH1")

    with pytest.raises(channel_reg.ChannelError, match="已存在"):
        channel_reg.create_channel(session, task.id, bearing.id, "CH1")


def test_create_channel_rejects_unknown_object(session, task):
    with pytest.raises(channel_reg.ChannelError, match="监测对象不存在"):
        channel_reg.create_channel(session, task.id, 999, "CH1")


def test_create_channel_requires_code(session, task, bearing):
    with pytest.raises(channel_reg.ChannelError, match="通道号"):
        channel_reg.create_channel(session, task.id, bearing.id, "  ")


def test_delete_channel_hides(session, task, bearing):
    row = channel_reg.create_channel(session, task.id, bearing.id, "CH1")

    channel_reg.delete_channel(session, row.id)

    assert channel_reg.list_channels(session, task.id) == []


def test_sync_channels_from_tree(session, task):
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(
        session, task.id, "component", "隔膜过辊轴承", parent_id=device.id, attrs={"类型": "轴承"}
    )
    tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "channel", "CH2", parent_id=comp.id)

    created = channel_reg.sync_from_tree(session, task.id)

    assert created == 2
    assert [r.code for r in channel_reg.list_channels(session, task.id)] == ["CH1", "CH2"]


# ---------------------------------------------------------------- 指标台账


def test_create_and_list_metric(session, task):
    metric_def.create_metric(session, task.id, "v_RMS", unit="mm/s", params={"band": [10, 1000]})

    rows = metric_def.list_metrics(session, task.id)

    assert [r.code for r in rows] == ["v_RMS"]
    assert rows[0].unit == "mm/s"
    assert rows[0].params == {"band": [10, 1000]}


def test_create_metric_rejects_duplicate(session, task):
    metric_def.create_metric(session, task.id, "v_RMS")

    with pytest.raises(metric_def.MetricDefError, match="已存在"):
        metric_def.create_metric(session, task.id, "v_RMS")


def test_create_metric_rejects_empty_code(session, task):
    with pytest.raises(metric_def.MetricDefError, match="指标码"):
        metric_def.create_metric(session, task.id, " ")


def test_update_metric_params(session, task):
    row = metric_def.create_metric(session, task.id, "a_Peak")

    updated = metric_def.update_metric(session, row.id, params={"window": "全时"}, unit="g")

    assert updated.params == {"window": "全时"}
    assert updated.unit == "g"


def test_delete_metric_hides(session, task):
    row = metric_def.create_metric(session, task.id, "a_RMS")

    metric_def.delete_metric(session, row.id)

    assert metric_def.list_metrics(session, task.id) == []


def test_seed_default_metrics(session, task):
    created = metric_def.seed_defaults(session, task.id)

    assert created == 3
    assert [r.code for r in metric_def.list_metrics(session, task.id)] == [
        "v_RMS",
        "a_Peak",
        "a_RMS",
    ]
    assert metric_def.seed_defaults(session, task.id) == 0
