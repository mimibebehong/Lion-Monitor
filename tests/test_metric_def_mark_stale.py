"""批次 B-2（复查 v2 问题 3）：改「指标定义默认值」必须把受影响的历史值标 ``stale``。

改 ``metric_def.params_default_json`` 等于改所有**跟随定义**的节点的生效参数。此前这里
既不 ``mark_stale`` 也不提示，趋势图把旧参数算的点和按新参数算的点画在同一条线上。

口径（``store.metric_assign.mark_stale_for_definition_change``）：

- 未固化（``params_locked=0``）且没有覆盖被改键的节点 → 旧值置 ``stale=1``；
- 已固化（``params_locked=1``）的节点参数是快照，定义默认值改了与它无关 → 不动；
- 只标记不重算；``update_metric`` 返回受影响条数，页面提示「待重算 N 条」。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.store import audit, metric_assign, metric_def, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store

FS = 32000.0
STAMP = "20261030100000"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    row = tasks.create_task(session, name="定义默认值", operator="马哥")
    metric_def.seed_defaults(session, row.id)
    return row


@pytest.fixture()
def batch(tmp_path) -> Path:
    """4 s 单通道批次（2 个 2 s 窗 → 每个指标 2 行）。"""
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * 4.0)) / FS
    samples = (0.8 * np.sin(2 * np.pi * 120 * t) + 0.2 * np.sin(2 * np.pi * 300 * t)).astype(
        "float32"
    )
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=samples,
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def _channel(session, task, code: str = "v_RMS") -> int:
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "metric", code, parent_id=ch1.id, attrs={"code": code})
    return ch1.id


def _definition(session, task) -> metric_def.MetricDefRow:
    row = metric_def.find_metric(session, task.id, "v_RMS")
    assert row is not None
    return row


def _imported(session, task, batch, **assign_kwargs):
    """建通道 + 授予 v_RMS + 导入一批 → (通道节点 id, 授予行)。"""
    ch1 = _channel(session, task)
    assigned = metric_assign.assign_metric(session, task.id, ch1, "v_RMS", **assign_kwargs)
    import_batch(session, task.id, batch, {"CH1": ch1})
    return ch1, assigned


def test_definition_default_change_marks_following_nodes_stale(session, task, batch):
    """跟随定义的节点：改默认 band_high → 历史 v_RMS 全部置 stale，并返回条数。"""
    ch1, _assigned = _imported(session, task, batch)
    assert metric_store.count_stale(session, task.id) == 0

    updated = metric_def.update_metric(
        session, _definition(session, task).id, defaults={"band_high": 500.0}
    )

    marked = metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS")
    assert marked == 2, "4 s / 2 s 窗 → 2 行历史值应全部待重算"
    assert updated.stale_marked == marked


def test_effective_params_follow_the_new_default(session, task, batch):
    """标记的同时生效参数确实变了（否则「待重算」就是空喊）。"""
    _ch1, assigned = _imported(session, task, batch)

    metric_def.update_metric(session, _definition(session, task).id, defaults={"band_high": 500.0})

    assert metric_assign.effective_params(session, assigned.id)["band_high"] == 500.0


def test_saving_the_same_defaults_again_does_not_recount(session, task, batch):
    """默认值没变（或再次保存同值）→ 不重复标记、返回 0。"""
    _imported(session, task, batch)
    definition = _definition(session, task)
    metric_def.update_metric(session, definition.id, defaults={"band_high": 500.0})
    before = metric_store.count_stale(session, task.id)

    again = metric_def.update_metric(session, definition.id, defaults={"band_high": 500.0})

    assert again.stale_marked == 0
    assert metric_store.count_stale(session, task.id) == before


def test_locked_node_is_not_affected_by_definition_change(session, task, batch):
    """已固化节点：参数是快照，定义默认值改了对它无效 → 历史值不动。"""
    ch1, assigned = _imported(session, task, batch)
    metric_assign.set_locked(session, assigned.id, True)
    # 造一批干净（stale=0）的历史值：重导同一批 → 同一时点覆盖，stale 清零
    metric_store.mark_stale(session, ch1, "v_RMS")
    import_batch(session, task.id, batch, {"CH1": ch1})
    before = metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS")

    updated = metric_def.update_metric(
        session, _definition(session, task).id, defaults={"band_high": 500.0}
    )

    assert updated.stale_marked == 0
    assert metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS") == before
    assert metric_assign.effective_params(session, assigned.id)["band_high"] != 500.0


def test_node_override_of_changed_key_is_not_marked(session, task, batch):
    """节点覆盖了被改的键 → 生效参数没变，不标（只标真正失效的点）。"""
    ch1, assigned = _imported(session, task, batch, params={"band_high": 300.0})
    metric_store.mark_stale(session, ch1, "v_RMS")
    import_batch(session, task.id, batch, {"CH1": ch1})
    before = metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS")

    updated = metric_def.update_metric(
        session, _definition(session, task).id, defaults={"band_high": 500.0}
    )

    assert updated.stale_marked == 0
    assert metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS") == before
    assert metric_assign.effective_params(session, assigned.id)["band_high"] == 300.0


def test_definition_change_is_written_to_change_log(session, task, batch):
    """留痕：谁改的、旧值/新值、影响条数（entity_type=metric_def）。"""
    _imported(session, task, batch)
    definition = _definition(session, task)

    metric_def.update_metric(session, definition.id, defaults={"band_high": 500.0})

    rows = audit.list_changes(session, entity_type="metric_def", entity_id=definition.id)
    fields = {row.field: row for row in rows}
    assert fields["params_default_json"].new_value["band_high"] == 500.0
    stale_row = fields["params_default_stale"]
    assert stale_row.new_value == 2
    assert stale_row.actor == "马哥"
    assert "2 条指标值" in stale_row.note


def test_unrelated_metric_definition_is_untouched(session, task, batch):
    """改 a_Peak 的定义默认值 → v_RMS 的历史值不受影响。"""
    ch1, _assigned = _imported(session, task, batch)
    a_peak = metric_def.find_metric(session, task.id, "a_Peak")
    assert a_peak is not None

    updated = metric_def.update_metric(session, a_peak.id, defaults={"window_s": 4.0})

    assert updated.stale_marked == 0
    assert metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS") == 0
