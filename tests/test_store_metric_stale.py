"""方案 A 第 4 条：参数 / 转速基准一变，该 (node_id, metric_code) 的旧值置 ``stale=1``。

本次只做标记，不做自动重算（重算链路属 S4 之后）。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import datasets, metric_assign, metric_def, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

STAMP = "2026-10-31 09:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


def _channel(session, tmp_path, code: str = "CH1") -> int:
    """建任务 → 设备 → 监测对象 → 通道节点，返回通道节点 id。"""
    task = tasks.create_task(session, name="置 stale")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
    for metric_code in ("v_RMS", "a_RMS"):
        tree.create_node(
            session,
            task.id,
            "metric",
            metric_code,
            parent_id=channel.id,
            attrs={"code": metric_code},
        )
    return channel.id


def _file(session, tmp_path, channel_node_id: int, code: str = "CH1"):
    task_id = tree.get_node(session, channel_node_id).task_id
    folder = tmp_path / f"ds{channel_node_id}"
    folder.mkdir(exist_ok=True)
    ds = datasets.create_dataset(session, task_id, "批次", folder, "2026-10-31 08:00:00")
    return datasets.add_file(session, ds.id, folder / f"{code}_Task1.dat", code, 32000.0, 64000)


def _rows(session, tmp_path, channel_node_id: int, codes: tuple[str, ...] = ("v_RMS",)):
    file_row = _file(session, tmp_path, channel_node_id)
    metric_store.upsert_node_values(
        session,
        file_row.id,
        STAMP,
        [
            NodeValue(node_id=channel_node_id, code=code, value=1.0 + index)
            for index, code in enumerate(codes)
        ],
    )
    task_id = tree.get_node(session, channel_node_id).task_id
    code = tree.get_node(session, channel_node_id).name
    return {row.metric_code: row for row in metric_store.list_values(session, task_id, code)}


def test_update_params_marks_that_node_metric_stale(session, tmp_path):
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    row = metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    _rows(session, tmp_path, channel_id)

    metric_assign.update_params(session, row.id, {"band_high": 500.0})

    fresh = {
        r.metric_code: r
        for r in metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    }
    assert fresh["v_RMS"].stale is True


def test_update_params_marks_only_the_changed_metric(session, tmp_path):
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    v_rms = metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    metric_assign.assign_metric(session, task_id, channel_id, "a_RMS")
    _rows(session, tmp_path, channel_id, codes=("v_RMS", "a_RMS"))

    metric_assign.update_params(session, v_rms.id, {"band_high": 500.0})

    rows = {
        r.metric_code: r
        for r in metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    }
    assert rows["v_RMS"].stale is True
    assert rows["a_RMS"].stale is False


def test_update_params_with_same_value_keeps_values_fresh(session, tmp_path):
    """参数没真变（1000 与 1000.0 视为同一个值）就不该把历史值置 stale。"""
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    row = metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    _rows(session, tmp_path, channel_id)

    metric_assign.update_params(session, row.id, {"band_high": 1000.0})

    fresh = metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    assert all(item.stale is False for item in fresh)


def test_set_locked_alone_does_not_mark_stale(session, tmp_path):
    """固化只换存储形式（写全量快照），生效值不变 → 历史值不算过期。"""
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    row = metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    _rows(session, tmp_path, channel_id)

    metric_assign.set_locked(session, row.id, True)

    fresh = metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    assert all(item.stale is False for item in fresh)


def test_set_node_rpm_writes_attr_and_marks_stale(session, tmp_path):
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    _rows(session, tmp_path, channel_id)

    marked = metric_assign.set_node_rpm(session, channel_id, 1480.0)

    assert marked == 1
    assert tree.get_node(session, channel_id).attrs[tree.RPM_ATTR] == "1480"
    fresh = metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    assert all(item.stale is True for item in fresh)


def test_set_node_rpm_same_value_marks_nothing(session, tmp_path):
    channel_id = _channel(session, tmp_path)
    _rows(session, tmp_path, channel_id)
    metric_assign.set_node_rpm(session, channel_id, 1480.0)
    # 重新算出新值（本次不重算，测试里手动写一份新鲜的）
    metric_store.upsert_node_values(
        session,
        _file(session, tmp_path, channel_id).id,
        "2026-10-31 10:00:00",
        [NodeValue(node_id=channel_id, code="v_RMS", value=2.0)],
    )

    assert metric_assign.set_node_rpm(session, channel_id, 1480.0) == 0


def test_set_node_rpm_on_object_node_marks_child_channels(session, tmp_path):
    """转速填在监测对象节点上 → 它下面各通道节点的旧值都过期。"""
    first = _channel(session, tmp_path)
    task_id = tree.get_node(session, first).task_id
    comp_id = tree.get_node(session, first).parent_id
    second = tree.create_node(session, task_id, "channel", "CH2", parent_id=comp_id).id
    _rows(session, tmp_path, first, codes=("v_RMS",))
    _rows(session, tmp_path, second, codes=("v_RMS",))

    marked = metric_assign.set_node_rpm(session, comp_id, 1500.0)

    assert marked == 2
    for node_id, code in ((first, "CH1"), (second, "CH2")):
        rows = metric_store.list_values(session, task_id, code, node_id=node_id)
        assert all(item.stale is True for item in rows)


def test_mark_stale_ignores_deleted_rows(session, tmp_path):
    channel_id = _channel(session, tmp_path)
    file_row = _file(session, tmp_path, channel_id)
    metric_store.upsert_node_values(
        session,
        file_row.id,
        STAMP,
        [NodeValue(node_id=channel_id, code="v_RMS", value=1.0)],
    )
    metric_store.delete_values_for_channel(session, file_row.id)

    assert metric_store.mark_stale(session, channel_id, "v_RMS") == 0


def test_definition_default_change_marks_stale_on_save(session, tmp_path):
    """节点只是把参数从「跟随定义」写成本节点覆盖：生效值变了就置 stale。"""
    channel_id = _channel(session, tmp_path)
    task_id = tree.get_node(session, channel_id).task_id
    metric_def.seed_defaults(session, task_id)
    row = metric_assign.assign_metric(session, task_id, channel_id, "v_RMS")
    _rows(session, tmp_path, channel_id)

    metric_assign.update_params(session, row.id, {"band_low": 20.0})

    fresh = metric_store.list_values(session, task_id, "CH1", node_id=channel_id)
    assert all(item.stale is True for item in fresh)
