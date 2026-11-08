"""批量补绑定（S4W-5）：给健康树里所有「通道 × 指标」补上参数绑定。

背景：早期数据/老模板导入的指标节点只有节点、没有 ``metric_assign`` 行，界面显示
「未绑定参数」，参数表单渲染不出来（改不了计算时长）。现场要求页面上有「一键绑定全部
指标参数」的入口——作用范围是**全树所有指标**，不是某几个通道。

口径：

- 只补缺失的（已有绑定的跳过），幂等：第二次返回 (0, [])
- 绑定行的 ``node_id`` 是**通道节点 id**（``assign_metric`` 的契约），不是指标节点 id
- 指标码既不是内置三指标、台账里也没有的，跳过并回报（不抛异常、不中断整批）
- 绑定只写空覆盖 ``{}`` = 跟随指标定义默认
"""

from __future__ import annotations

import pytest

from lion_monitor.store import db as store_db
from lion_monitor.store import metric_assign, metric_def, tasks, tree


@pytest.fixture()
def scene(tmp_path):
    """一个任务：两个通道，指标节点分别有/无绑定，另有一个台账里没有的指标码。"""
    engine = store_db.make_engine(tmp_path / "bind.db")
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="批量绑定任务")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "辊压机", parent_id=device.id)
        ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        ch2 = tree.create_node(session, task.id, "channel", "CH2", parent_id=comp.id)
        metric_def.seed_defaults(session, task.id)
        nodes = {}
        for channel, codes in ((ch1, ("v_RMS", "a_Peak")), (ch2, ("v_RMS",))):
            for code in codes:
                nodes[(channel.name, code)] = tree.create_node(
                    session, task.id, "metric", code, parent_id=channel.id, attrs={"code": code}
                )
        # 已绑定的一条（模拟历史上已装配过的通道）
        metric_assign.assign_metric(session, task.id, ch2.id, "v_RMS")
        ids = {"task": task.id, "ch1": ch1.id, "ch2": ch2.id, "nodes": nodes}
    return engine, factory, ids


def test_missing_bindings_lists_only_unbound(scene):
    _engine, factory, ids = scene
    with factory() as session:
        missing = metric_assign.missing_bindings(session, ids["task"])

    got = {(channel_name, code) for _cid, channel_name, code in missing}
    assert got == {("CH1", "v_RMS"), ("CH1", "a_Peak")}


def test_bind_missing_is_idempotent_and_uses_metric_node_id(scene):
    _engine, factory, ids = scene
    with store_db.session_scope(factory) as session:
        created, skipped = metric_assign.bind_missing(session, ids["task"])
    assert (created, skipped) == (2, [])

    with factory() as session:
        rows = metric_assign.list_assignments(session, ids["task"])
        nodes = tree.list_nodes(session, ids["task"])
    # 绑定行挂在指标节点上（2026-11-02：参数配置下放到指标节点）
    metric_ids = {node.id for node in nodes if node.node_type == "metric"}
    assert {row.node_id for row in rows} == metric_ids
    assert sorted(row.code for row in rows) == ["a_Peak", "v_RMS", "v_RMS"]
    ch1_metric_ids = {
        node.id for node in nodes if node.node_type == "metric" and node.parent_id == ids["ch1"]
    }
    ch1 = [row for row in rows if row.node_id in ch1_metric_ids]
    assert all(row.params == {} and row.params_locked is False for row in ch1)

    # 幂等：再跑一次没有新增
    with store_db.session_scope(factory) as session:
        assert metric_assign.bind_missing(session, ids["task"]) == (0, [])
    with factory() as session:
        assert len(metric_assign.list_assignments(session, ids["task"])) == 3


def test_bind_missing_skips_unknown_code(scene):
    """指标码不在台账也不是内置：跳过并回报，不抛异常。"""
    engine, factory, ids = scene
    with store_db.session_scope(factory) as session:
        tree.create_node(
            session,
            ids["task"],
            "metric",
            "no_such_code",
            parent_id=ids["ch1"],
            attrs={"code": "no_such_code"},
        )

    with store_db.session_scope(factory) as session:
        created, skipped = metric_assign.bind_missing(session, ids["task"])

    assert created == 2
    assert skipped == ["no_such_code"]
    with factory() as session:
        codes = {row.code for row in metric_assign.list_assignments(session, ids["task"])}
    assert "no_such_code" not in codes
