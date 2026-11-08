"""按节点手动重算（S4A）：复用导入链路，按**当前生效参数**重算历史点位。

口径（``docs/kanban/bodies/s4a_brief.md``）：

- 重算区间 = 该通道节点的全部批次源文件（跨数据集）；
- 只算该节点树上挂的指标（``code`` 给了就只算那一个）；
- 同一 ``(通道, 节点, 指标, computed_at)`` 覆盖为新值并置 ``stale=0``；窗长/步长改了以后
  不再产生的旧点位软删（不再挂在「待重算」上）；
- 读不了的文件、缺转速 / 没有计算实现的指标逐个记原因，不静默吞掉、不出假值；
- 重算留一条审计（``change_log``，entity_type=``metric_value``）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features import recompute
from lion_monitor.features.metrics import compute_metric_series
from lion_monitor.features.pipeline import import_batch
from lion_monitor.ingest.vibration import read_vibration
from lion_monitor.store import audit, metric_assign, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store

FS = 32000.0
STAMP = "20261029152834"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="重算A")


@pytest.fixture()
def batch(tmp_path) -> Path:
    """20 s 单通道批次：120 Hz + 300 Hz 双音（每 2 s 窗量级稳定）。"""
    folder = tmp_path / "批次" / STAMP
    t = np.arange(int(FS * 20.0)) / FS
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


def _channel(session, task, codes: tuple[str, ...] = ("v_RMS",)) -> int:
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch1 = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    for code in codes:
        tree.create_node(session, task.id, "metric", code, parent_id=ch1.id, attrs={"code": code})
    return ch1.id


def _imported_with_stale(session, task, batch, codes=("v_RMS",), new_params=None):
    """导入一批 → 改参数（旧值置 stale）→ 返回通道节点 id。"""
    ch1 = _channel(session, task, codes)
    assigned = [metric_assign.assign_metric(session, task.id, ch1, code) for code in codes]
    import_batch(session, task.id, batch, {"CH1": ch1})
    for row in assigned:
        metric_assign.update_params(session, row.id, new_params or {"window_s": 5.0, "step_s": 2.0})
    return ch1


def test_plan_counts_stale_points_and_files(session, task, batch):
    ch1 = _imported_with_stale(session, task, batch)

    plan = recompute.recompute_plan(session, task.id, ch1)

    assert plan.node_id == ch1
    assert plan.codes == ["v_RMS"]
    assert plan.stale_count == 10  # 2 s / 2 s → 20 s 出 10 点，全部待重算
    assert plan.file_count == 1
    assert plan.point_estimate == 8  # 改后 5 s / 2 s → (20-5)//2+1 = 8 点
    assert plan.estimate_seconds >= 1.0


def test_recompute_overwrites_values_with_current_params_and_clears_stale(session, task, batch):
    ch1 = _imported_with_stale(session, task, batch)
    before = {
        row.computed_at: row
        for row in metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    }
    assert len(before) == 10
    assert all(row.stale for row in before.values())

    result = recompute.recompute_node(session, task.id, ch1)

    assert result.recomputed == 8
    assert result.retired == 10  # 旧 2 s 窗的 10 个点位一个新窗中心都对不上 → 全部作废
    assert result.stale_before == 10
    assert result.stale_after == 0
    assert result.problems == []

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    assert len(rows) == 8
    assert all(row.stale is False for row in rows)
    # 时间戳按新窗中心（5 s 窗 / 2 s 步长 → 2.5,4.5,…,16.5 s）
    assert [row.computed_at for row in rows] == [
        f"2026-10-29 15:28:{second:02d}.500000" for second in (36, 38, 40, 42, 44, 46, 48, 50)
    ]
    # 值 = 用当前参数直接算的结果
    record = read_vibration(batch / f"{STAMP}-CH1_Task1.dat")
    expected = compute_metric_series(
        record.samples, FS, params={"window_s": 5.0, "step_s": 2.0}, codes=["v_RMS"]
    )["v_RMS"]
    assert [row.value for row in rows] == pytest.approx([value for _offset, value in expected])
    assert all(row.params_hash for row in rows)

    changes = audit.list_changes(session, entity_type="metric_value", entity_id=ch1)
    assert len(changes) == 1
    assert changes[0].field == "recompute"
    assert changes[0].new_value["recomputed"] == 8
    assert changes[0].entity_text.startswith("指标值（重算）")  # 变更历史里能按实体筛出来


def test_recompute_same_window_keeps_point_count_and_overwrites_in_place(session, task, batch):
    """窗长不变、只改带通 → 点位一一对应，直接覆盖（旧值换了新值）。"""
    ch1 = _channel(session, task)
    assigned = metric_assign.assign_metric(session, task.id, ch1, "v_RMS")
    import_batch(session, task.id, batch, {"CH1": ch1})
    metric_assign.update_params(session, assigned.id, {"band_high": 500.0})
    old = {
        row.computed_at: row
        for row in metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    }
    assert len(old) == 10 and all(row.stale for row in old.values())

    result = recompute.recompute_node(session, task.id, ch1)

    assert (result.recomputed, result.retired) == (10, 0)
    fresh = {
        row.computed_at: row
        for row in metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=ch1)
    }
    assert sorted(fresh) == sorted(old)
    assert all(row.stale is False for row in fresh.values())
    assert all(row.id == old[stamp].id for stamp, row in fresh.items())  # 原地覆盖，id 不变
    assert any(row.params_hash != old[stamp].params_hash for stamp, row in fresh.items()), (
        "参数指纹应随当前生效参数更新"
    )


def test_recompute_can_limit_to_one_metric_code(session, task, batch):
    ch1 = _imported_with_stale(session, task, batch, codes=("v_RMS", "a_RMS"))
    assert metric_store.count_stale(session, task.id, node_id=ch1) == 20

    result = recompute.recompute_node(session, task.id, ch1, code="v_RMS")

    assert result.codes == ["v_RMS"]
    assert metric_store.count_stale(session, task.id, node_id=ch1, code="v_RMS") == 0
    assert metric_store.count_stale(session, task.id, node_id=ch1, code="a_RMS") == 10


def test_recompute_reports_missing_file_and_keeps_stale(session, task, batch):
    ch1 = _imported_with_stale(session, task, batch)
    (batch / f"{STAMP}-CH1_Task1.dat").unlink()

    result = recompute.recompute_node(session, task.id, ch1)

    assert result.recomputed == 0
    assert any("读不了" in item for item in result.problems)
    assert result.stale_after == result.stale_before == 10


def test_plan_and_recompute_refuse_channel_without_metrics(session, task):
    ch1 = _channel(session, task, codes=())

    plan = recompute.recompute_plan(session, task.id, ch1)
    result = recompute.recompute_node(session, task.id, ch1)

    assert plan.codes == [] and plan.stale_count == 0
    assert result.recomputed == 0
    assert any("没有可重算的指标" in item for item in result.problems)


def test_recompute_node_reports_progress(session, task, batch):
    """重算过程按「文件 × 指标」推进，回调单调递增并以 1.0 收尾（2026-11-03 要进度条）。"""
    ch1 = _imported_with_stale(session, task, batch)
    calls: list[tuple[float, str]] = []
    result = recompute.recompute_node(
        session,
        task.id,
        ch1,
        progress=lambda fraction, text="": calls.append((fraction, text)),
    )

    assert result.recomputed > 0
    assert calls, "应该回调进度"
    fractions = [fraction for fraction, _ in calls]
    assert fractions == sorted(fractions), fractions
    assert fractions[-1] == pytest.approx(1.0)
    assert all(0.0 <= f <= 1.0 for f in fractions)
    assert any("重算" in text or "·" in text for _, text in calls)
