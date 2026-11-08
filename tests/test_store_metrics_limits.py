"""指标配置（store.metric_assign）+ 阈值设置（store.limits）子模块测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from lion_monitor.store import db, limits, metric_assign, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import MetricLimit


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def chain(session):
    task = tasks.create_task(session, name="卷绕产线振动监测")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for metric_code in ("v_RMS", "a_Peak", "a_RMS", "峰值因子"):
        tree.create_node(
            session,
            task.id,
            "metric",
            metric_code,
            parent_id=ch.id,
            attrs={"code": metric_code},
        )
    return task, root, device, comp, ch


# ---------------------------------------------------------------- 指标配置


def test_assign_metric_to_channel(session, chain):
    task, _, _, _, ch = chain

    row = metric_assign.assign_metric(
        session, task.id, ch.id, "v_RMS", unit="mm/s", params={"band": [10, 1000]}
    )

    assert row.code == "v_RMS"
    assert row.unit == "mm/s"
    assert row.params == {"band": [10, 1000]}
    assert row.channel_name == "CH1"


def test_assign_metric_rejects_non_channel(session, chain):
    task, _, _, comp, _ = chain

    with pytest.raises(metric_assign.MetricAssignError, match="通道"):
        metric_assign.assign_metric(session, task.id, comp.id, "v_RMS")


def test_assign_metric_rejects_unknown_code(session, chain):
    task, _, _, _, ch = chain

    with pytest.raises(metric_assign.MetricAssignError, match="指标码"):
        metric_assign.assign_metric(session, task.id, ch.id, "峰值因子")


def test_assign_metric_rejects_duplicate(session, chain):
    task, _, _, _, ch = chain
    metric_assign.assign_metric(session, task.id, ch.id, "v_RMS")

    with pytest.raises(metric_assign.MetricAssignError, match="已授予"):
        metric_assign.assign_metric(session, task.id, ch.id, "v_RMS")


def test_list_assignments_and_update_params(session, chain):
    task, _, _, _, ch = chain
    row = metric_assign.assign_metric(session, task.id, ch.id, "a_Peak", unit="g")

    metric_assign.update_params(session, row.id, {"window": "全时"})

    rows = metric_assign.list_assignments(session, task.id)
    assert len(rows) == 1
    assert rows[0].params == {"window": "全时"}
    assert rows[0].unit == "g"


def test_unassign_hides_from_list(session, chain):
    task, _, _, _, ch = chain
    row = metric_assign.assign_metric(session, task.id, ch.id, "a_RMS")

    metric_assign.unassign(session, row.id)

    assert metric_assign.list_assignments(session, task.id) == []


# ---------------------------------------------------------------- 阈值


def test_set_limit_on_channel_reports_source(session, chain):
    task, _, _, _, ch = chain

    limits.set_limit(session, task.id, ch.id, "v_RMS", warn=2.8, alert=4.5)

    rows = limits.list_limits(session, task.id)
    assert len(rows) == 1
    assert rows[0].warn == 2.8
    assert rows[0].alert == 4.5
    assert rows[0].source == "通道级"
    assert rows[0].node_name == "CH1"


def test_resolve_inherits_from_device_then_task(session, chain):
    task, root, device, _, ch = chain
    limits.set_limit(session, task.id, device.id, "v_RMS", warn=4.5, alert=11.2)

    warn, alert, _, source = limits.resolve_limit(session, task.id, ch.id, "v_RMS")
    assert (warn, alert, source) == (4.5, 11.2, "设备级")

    limits.set_limit(session, task.id, root.id, "a_Peak", warn=1.5, alert=3.0)
    warn2, alert2, _, source2 = limits.resolve_limit(session, task.id, ch.id, "a_Peak")
    assert (warn2, alert2, source2) == (1.5, 3.0, "任务默认")


def test_resolve_returns_none_when_unset(session, chain):
    task, _, _, _, ch = chain

    assert limits.resolve_limit(session, task.id, ch.id, "a_RMS") is None


def test_set_limit_rejects_warn_not_below_alert(session, chain):
    task, _, _, _, ch = chain

    with pytest.raises(limits.LimitError, match="warn"):
        limits.set_limit(session, task.id, ch.id, "v_RMS", warn=5.0, alert=4.5)


def test_set_limit_accepts_lower_direction_warn_above_alert(session, chain):
    """「越小越差」：warn=3（较轻，数值大）/ alert=1（更严重，数值小）必须能存。"""
    task, _, _, _, ch = chain

    row = limits.set_limit(session, task.id, ch.id, "油压", warn=3.0, alert=1.0, direction="<")

    assert (row.warn, row.alert, row.direction) == (3.0, 1.0, "<")


def test_set_limit_rejects_upper_direction_with_warn_above_alert(session, chain):
    task, _, _, _, ch = chain

    with pytest.raises(limits.LimitError) as err:
        limits.set_limit(session, task.id, ch.id, "v_RMS", warn=4.5, alert=2.8, direction=">")

    text = str(err.value)
    assert "上限" in text
    assert "warn < alert" in text


def test_set_limit_upserts_same_node_and_metric(session, chain):
    task, _, _, _, ch = chain
    limits.set_limit(session, task.id, ch.id, "v_RMS", warn=2.8, alert=4.5)

    limits.set_limit(session, task.id, ch.id, "v_RMS", warn=3.0, alert=6.0)

    rows = limits.list_limits(session, task.id)
    assert len(rows) == 1
    assert (rows[0].warn, rows[0].alert) == (3.0, 6.0)


def test_delete_limit_removes_row(session, chain):
    task, _, _, _, ch = chain
    row = limits.set_limit(session, task.id, ch.id, "v_RMS", warn=2.8, alert=4.5)

    limits.delete_limit(session, row.id)

    assert limits.list_limits(session, task.id) == []
    assert limits.resolve_limit(session, task.id, ch.id, "v_RMS") is None


@pytest.fixture()
def engine_factory(tmp_path):
    counter = {"n": 0}

    def _make():
        counter["n"] += 1
        engine = make_engine(tmp_path / f"m{counter['n']}.db")
        init_db(engine)
        return engine

    return _make


@pytest.fixture()
def metric_chain(session):
    """任务 → 设备 → 对象 → CH1 → v_RMS，返回各层节点。"""
    task = tasks.create_task(session, name="阈值粒度任务")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
    )
    return {"task": task, "channel": channel, "metric": metric}


def test_limit_on_metric_node_reports_metric_source(session, metric_chain):
    """阈值挂在指标节点上时，来源显示「指标级」。"""
    limits.set_limit(
        session, metric_chain["task"].id, metric_chain["metric"].id, "v_RMS", 1.5, 3.0, ">"
    )

    hit = limits.resolve_limit(session, metric_chain["task"].id, metric_chain["metric"].id, "v_RMS")

    assert hit is not None
    assert (hit[0], hit[1], hit[3]) == (1.5, 3.0, "指标级")


def test_metric_node_limit_overrides_channel_limit(session, metric_chain):
    """指标级优先：通道级 2.8/4.5 与指标级 1.5/3.0 同时存在时取指标级。"""
    limits.set_limit(
        session, metric_chain["task"].id, metric_chain["channel"].id, "v_RMS", 2.8, 4.5, ">"
    )
    limits.set_limit(
        session, metric_chain["task"].id, metric_chain["metric"].id, "v_RMS", 1.5, 3.0, ">"
    )

    hit = limits.resolve_limit(session, metric_chain["task"].id, metric_chain["metric"].id, "v_RMS")

    assert (hit[0], hit[1], hit[3]) == (1.5, 3.0, "指标级")


def test_migrate_limits_pushes_channel_limits_to_metric_nodes(engine_factory):
    """迁移：挂在通道节点上的阈值搬到同名指标节点，重复跑不重复搬。"""
    engine = engine_factory()
    with make_session_factory(engine)() as session:
        task = tasks.create_task(session, name="迁移任务")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric = tree.create_node(
            session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
        )
        limits.set_limit(session, task.id, channel.id, "v_RMS", 2.8, 4.5, ">")

        moved = db.migrate_limits_to_metric_nodes(engine)

        assert moved == 1
        row = session.execute(select(MetricLimit)).scalars().one()
        assert row.node_id == metric.id
        assert db.migrate_limits_to_metric_nodes(engine) == 0
