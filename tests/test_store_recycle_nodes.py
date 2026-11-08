"""P6-3 节点级回收站（G6）：快照留痕、恢复、模板替换逐条留痕、7 天保留期清理。

对应验收（docs/kanban/bodies/p6_3_brief.md）：
1. 删除带 2 个子节点 + 1 个指标授权的节点 → ``deletion_log`` 1 行，payload 含完整子树；
2. 回收站列出该条，恢复后节点 / 子树 / 指标授权全部回来，且 ``deleted_at`` 为空；
3. 模板整树替换 → 旧节点逐条进 deletion_log（条数 = 被替换节点数）；
4. 保留期：8 天前的记录启动清理后消失，6 天前的不受影响。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from lion_monitor.store import (
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    recycle,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db
from lion_monitor.store.models import DeletionLog, MetricAssign, MetricLimit, TaskNode


@pytest.fixture()
def engine(tmp_path):
    eng = store_db.make_engine(tmp_path / "recycle_nodes.db")
    store_db.init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    with store_db.make_session_factory(engine)() as sess:
        yield sess


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="卷绕产线振动监测")


def build_winding_line(session, task):
    """设备 → 监测对象 → 通道 → 指标：一棵 5 节点的小树（含 1 个授权、1 条阈值）。"""
    obj = object_registry.create_object(session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    ch_reg = channel_reg.create_channel(session, task.id, obj.id, "CH1")
    metric_def.seed_defaults(session, task.id)
    defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    component = tree.create_node(
        session,
        task.id,
        "component",
        "隔膜过辊轴承",
        parent_id=device.id,
        attrs={"类型": "轴承"},
        ref_id=obj.id,
    )
    channel = tree.create_node(
        session, task.id, "channel", "CH1", parent_id=component.id, ref_id=ch_reg.id
    )
    metric = tree.create_node(
        session,
        task.id,
        "metric",
        "v_RMS",
        parent_id=channel.id,
        attrs={"code": "v_RMS", "单位": "mm/s"},
        ref_id=defs["v_RMS"].id,
    )
    metric_assign.assign_metric(session, task.id, channel.id, "v_RMS", params={"band_high": 2000})
    limits.set_limit(session, task.id, metric.id, "v_RMS", 1.8, 2.8)
    return root, device, component, channel, metric


def _snapshot_children(node: dict) -> list[dict]:
    return list(node.get("children") or [])


# ---------------------------------------------------------------- 验收 1


def test_soft_delete_node_writes_subtree_snapshot(session, task):
    _root, _device, component, channel, metric = build_winding_line(session, task)

    tree.soft_delete_node(session, component.id, operator="小智")

    logs = [log for log in tasks.list_deletion_logs(session) if log.entity_type == "task_node"]
    assert len(logs) == 1
    log = logs[0]
    assert log.entity_id == component.id
    assert log.entity_name == "卷绕产线振动监测 / 卷绕产线 / 隔膜过辊轴承"
    assert log.mode == "soft"
    assert log.operator == "小智"

    payload = json.loads(log.payload)
    snapshot = payload["node"]
    assert snapshot["id"] == component.id
    assert snapshot["node_type"] == "component"
    assert snapshot["attrs"] == {"类型": "轴承"}
    children = _snapshot_children(snapshot)
    assert [child["name"] for child in children] == ["CH1"]
    assert [child["name"] for child in _snapshot_children(children[0])] == ["v_RMS"]
    assert [row["metric_code"] for row in payload["assigns"]] == ["v_RMS"]
    assert payload["assigns"][0]["node_id"] == metric.id  # 参数卡挂在指标节点
    assert [row["metric_code"] for row in payload["limits"]] == ["v_RMS"]
    assert payload["limits"][0]["node_id"] == metric.id

    # 授权与阈值随节点一起进回收站（软删），不再是「幽灵授权」
    assign_id = payload["assigns"][0]["id"]
    limit_id = payload["limits"][0]["id"]
    assert session.get(MetricAssign, assign_id).deleted_at is not None
    assert session.get(MetricLimit, limit_id).deleted_at is not None
    assert metric_assign.list_assignments(session, task.id) == []
    assert limits.list_limits(session, task.id) == []


def test_soft_delete_node_sets_expires_at_seven_days(session, task):
    _root, _device, component, *_ = build_winding_line(session, task)

    tree.soft_delete_node(session, component.id)

    log = [row for row in tasks.list_deletion_logs(session) if row.entity_type == "task_node"][0]
    assert log.expires_at is not None
    assert (log.expires_at - log.created_at).days == recycle.RECYCLE_RETENTION_DAYS == 7


# ---------------------------------------------------------------- 验收 2


def test_recycle_lists_node_path_and_remaining_days(session, task):
    _root, _device, component, *_ = build_winding_line(session, task)
    tree.soft_delete_node(session, component.id)

    items = recycle.list_recycle_items(session)

    assert len(items) == 1
    item = items[0]
    assert item.entity_type == "task_node"
    assert item.entity_label == "节点"
    assert item.entity_id == component.id
    assert item.entity_name == "卷绕产线振动监测 / 卷绕产线 / 隔膜过辊轴承"
    assert item.remaining_days == 7
    assert item.deleted_at is not None


def test_restore_node_brings_back_subtree_and_assigns(session, task):
    _root, _device, component, channel, metric = build_winding_line(session, task)
    tree.soft_delete_node(session, component.id)
    item = recycle.list_recycle_items(session)[0]

    result = recycle.restore_item(session, item.log_id)

    assert result.ok is True
    assert "已恢复" in result.message
    assert [node.name for node in tree.list_nodes(session, task.id)] == [
        "卷绕产线振动监测",
        "卷绕产线",
        "隔膜过辊轴承",
        "CH1",
        "v_RMS",
    ]
    assert tree.get_node(session, component.id).deleted_at is None
    assert tree.get_node(session, channel.id).deleted_at is None
    assert tree.get_node(session, metric.id).deleted_at is None
    assigns = metric_assign.list_assignments(session, task.id)
    assert [(row.node_id, row.code) for row in assigns] == [(metric.id, "v_RMS")]
    assert assigns[0].params == {"band_high": 2000}
    assert [(row.node_id, row.metric_code) for row in limits.list_limits(session, task.id)] == [
        (metric.id, "v_RMS")
    ]
    assert recycle.list_recycle_items(session) == []

    restore_logs = [log for log in tasks.list_deletion_logs(session) if log.mode == "restore"]
    assert len(restore_logs) == 1
    assert restore_logs[0].entity_id == component.id
    assert json.loads(restore_logs[0].payload)["restored_from"] == item.log_id


def test_restore_node_with_missing_parent_falls_back_to_task_root(session, task):
    root, device, component, channel, _metric = build_winding_line(session, task)
    tree.soft_delete_node(session, component.id)
    log = [row for row in tasks.list_deletion_logs(session) if row.entity_type == "task_node"][0]
    tree.soft_delete_node(session, device.id)  # 父节点也删了

    result = recycle.restore_item(session, log.id)

    assert result.ok is True
    assert "任务根" in result.message
    restored = tree.get_node(session, component.id)
    assert restored.deleted_at is None
    assert restored.parent_id == root.id
    assert tree.get_node(session, channel.id).parent_id == component.id


# ---------------------------------------------------------------- 验收 3


def test_template_replace_logs_every_replaced_node(session, task):
    build_winding_line(session, task)
    payload = tree.export_template(session, task.id)
    alive_before = tree.list_nodes(session, task.id)

    tree.import_template(session, task.id, payload)

    logs = [row for row in tasks.list_deletion_logs(session) if row.entity_type == "task_node"]
    assert len(logs) == len(alive_before) == 5
    assert {row.entity_id for row in logs} == {node.id for node in alive_before}
    # 每条留痕都是单节点快照（逐节点留痕，不是一整棵静默批量删）
    assert all(len(json.loads(row.payload)["node"].get("children") or []) == 0 for row in logs)


# ---------------------------------------------------------------- 验收 4


def _age_log(session, log_id: int, days: int) -> None:
    """把一条 task_node 留痕及其软删行改成 ``days`` 天前。"""
    log = session.get(DeletionLog, log_id)
    when = datetime.now() - timedelta(days=days)
    log.created_at = when
    log.expires_at = when + timedelta(days=recycle.RECYCLE_RETENTION_DAYS)
    payload = json.loads(log.payload)
    ids = [payload["node"]["id"]]
    for assign in payload["assigns"]:
        session.get(MetricAssign, assign["id"]).deleted_at = when
    for limit in payload["limits"]:
        session.get(MetricLimit, limit["id"]).deleted_at = when
    for node_id in ids:
        session.get(TaskNode, node_id).deleted_at = when
    session.commit()


def test_startup_purge_removes_expired_records_only(engine, session, task):
    _root, _device, component, channel, _metric = build_winding_line(session, task)
    tree.soft_delete_node(session, component.id)
    expired = [row for row in tasks.list_deletion_logs(session) if row.entity_type == "task_node"][
        0
    ]
    _age_log(session, expired.id, 8)
    expired_assign = json.loads(expired.payload)["assigns"]
    assert expired_assign

    # 另建一棵树，删掉后只「老 6 天」——不该被清
    fresh_node = tree.create_node(
        session, task.id, "device", "包装线", parent_id=tree.root_node(session, task.id).id
    )
    tree.soft_delete_node(session, fresh_node.id)
    fresh = [
        row
        for row in tasks.list_deletion_logs(session)
        if row.entity_type == "task_node" and row.entity_id == fresh_node.id
    ][0]
    _age_log(session, fresh.id, 6)

    store_db.init_db(engine)  # 等价于应用启动

    session.expunge_all()
    assert session.get(DeletionLog, expired.id) is None
    assert session.get(TaskNode, component.id) is None
    assert session.get(TaskNode, channel.id) is None
    assert session.get(MetricAssign, expired_assign[0]["id"]) is None

    assert session.get(DeletionLog, fresh.id) is not None
    assert session.get(TaskNode, fresh_node.id) is not None
    assert recycle.list_recycle_items(session)[0].entity_id == fresh_node.id


def test_purge_keeps_alive_children_of_expired_parent(engine, session, task):
    """父节点留痕过期、但下面挂了未删的子节点：父行不能硬删（否则活子树悬空）。"""
    _root, device, component, *_ = build_winding_line(session, task)
    tree.soft_delete_node(session, component.id)
    comp_log = [row for row in tasks.list_deletion_logs(session) if row.entity_type == "task_node"][
        0
    ]
    # 把整棵子树恢复回来后再删设备：此时设备留痕覆盖 component / channel / metric
    recycle.restore_item(session, comp_log.id)
    tree.soft_delete_node(session, device.id)
    device_log = [
        row
        for row in tasks.list_deletion_logs(session)
        if row.entity_type == "task_node" and row.entity_id == device.id
    ][0]
    # 模拟「设备删了之后又在其下新建了一个活节点」（如从模板补建）
    child = tree.create_node(
        session,
        task.id,
        "device",
        "补建设备",
        parent_id=tree.root_node(session, task.id).id,
    )
    session.get(TaskNode, child.id).parent_id = device.id
    session.commit()
    _age_log(session, device_log.id, 9)

    store_db.init_db(engine)

    session.expunge_all()
    assert session.get(TaskNode, device.id) is not None
    assert session.get(TaskNode, device.id).deleted_at is not None
    assert tree.get_node(session, child.id).deleted_at is None
    assert session.get(DeletionLog, device_log.id) is None


# ---------------------------------------------------------------- 保留期常量


def test_retention_days_default_and_env_override(monkeypatch):
    monkeypatch.delenv(recycle.RETENTION_ENV, raising=False)
    assert recycle.retention_days() == 7

    monkeypatch.setenv(recycle.RETENTION_ENV, "3")
    assert recycle.retention_days() == 3
    base = datetime(2026, 11, 1, 12, 0, 0)
    assert recycle.expiry_of(base) == base + timedelta(days=3)

    monkeypatch.setenv(recycle.RETENTION_ENV, "不是数字")
    assert recycle.retention_days() == 7
