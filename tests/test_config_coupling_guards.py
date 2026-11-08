"""配置项联动（马哥 2026-11-02 评审要求 ②）：

指标定义「启用=否」必须真的停算，删除仍装配的定义必须被拦住，删除监测对象要把
它名下的通道解绑回自由态——否则页面上的开关和删除按钮只是"看起来生效"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features import pipeline  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    channel_reg,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import (  # noqa: E402
    db as store_db,
)


@pytest.fixture()
def env(tmp_path):
    engine = store_db.make_engine(str(tmp_path / "t.db"))
    store_db.init_db(engine)
    with Session(engine) as session:
        task = tasks.create_task(session, name="T1")
        metric_def.seed_defaults(session, task.id)
        root = tree.root_node(session, task.id)
        device = tree.create_node(
            session, task_id=task.id, parent_id=root.id, node_type="device", name="产线"
        )
        obj = object_registry.create_object(session, task_id=task.id, name="轴承A", obj_type="轴承")
        component = tree.create_node(
            session,
            task_id=task.id,
            parent_id=device.id,
            node_type="component",
            name="轴承A",
            ref_type="monitor_object",
            ref_id=obj.id,
        )
        channel = channel_reg.create_channel(session, task_id=task.id, code="CH1", object_id=obj.id)
        channel_node = tree.create_node(
            session,
            task_id=task.id,
            parent_id=component.id,
            node_type="channel",
            name="CH1",
            ref_type="channel_reg",
            ref_id=channel.id,
        )
        defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
        nodes = {}
        for code in ("v_RMS", "a_Peak"):
            node = tree.create_node(
                session,
                task_id=task.id,
                parent_id=channel_node.id,
                node_type="metric",
                name=code,
                ref_type="metric_def",
                ref_id=defs[code].id,
                attrs={"code": code},
            )
            metric_assign.assign_metric(session, task_id=task.id, node_id=node.id, code=code)
            nodes[code] = node
        yield {
            "session": session,
            "task": task,
            "object": obj,
            "channel": channel,
            "channel_node": channel_node,
            "defs": defs,
            "metric_nodes": nodes,
        }


def test_disabled_metric_definition_is_not_computed(env):
    session = env["session"]
    metric_def.update_metric(session, env["defs"]["v_RMS"].id, enabled=False)

    jobs, skipped = pipeline.metric_jobs(session, env["task"].id)
    assert [job.code for job in jobs] == ["a_Peak"], "停用的 v_RMS 不应再产出计算任务"
    reasons = {item.code: item.reason for item in skipped}
    assert "停用" in reasons["v_RMS"]


def test_enabled_flag_toggle_back_restores_computation(env):
    session = env["session"]
    metric_def.update_metric(session, env["defs"]["v_RMS"].id, enabled=False)
    metric_def.update_metric(session, env["defs"]["v_RMS"].id, enabled=True)

    jobs, skipped = pipeline.metric_jobs(session, env["task"].id)
    assert sorted(job.code for job in jobs) == ["a_Peak", "v_RMS"]
    assert [item.code for item in skipped] == []


def test_delete_metric_refuses_while_still_assembled(env):
    session = env["session"]
    with pytest.raises(metric_def.MetricDefError) as info:
        metric_def.delete_metric(session, env["defs"]["v_RMS"].id)
    assert "健康树" in str(info.value)
    # 定义仍在（没有被软删）
    codes = {row.code for row in metric_def.list_metrics(session, env["task"].id)}
    assert "v_RMS" in codes


def test_delete_metric_allowed_after_node_removed(env):
    session = env["session"]
    tree.soft_delete_node(session, env["metric_nodes"]["v_RMS"].id)
    metric_def.delete_metric(session, env["defs"]["v_RMS"].id)
    codes = {row.code for row in metric_def.list_metrics(session, env["task"].id)}
    assert "v_RMS" not in codes


def test_delete_object_releases_its_channels(env):
    session = env["session"]
    object_registry.delete_object(session, env["object"].id)

    channels = channel_reg.list_channels(session, env["task"].id)
    assert [row.code for row in channels] == ["CH1"], "通道是资产，不随对象删除"
    assert channels[0].object_id is None, "删对象后其通道必须回到自由态（object_id=NULL）"
