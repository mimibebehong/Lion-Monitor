"""旧参数值（``stale=1``）的统计与「按当前参数重算」落库口径（S4A）。

口径（马哥 2026-11-02 定，见 ``docs/kanban/bodies/s4a_brief.md``）：

- 「待重算 N 条」= 该任务 / 该节点下 ``stale=1`` 且未删的指标值行数（``count_stale``）；
- 重算后同一 ``(通道, 节点, 指标, computed_at)`` **直接覆盖为新值并置 ``stale=0``**
  （与导入「同一时点覆盖」口径一致，id 不变）；
- 当前参数下不再产生的旧点位（窗长 / 步长改了）不能一直挂在「待重算」上 → 软删留痕；
- 别的节点、别的指标、非 stale 的行一律不动。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue
from lion_monitor.store.models import MetricValue

FS = 32000.0
T1, T2, T3, T4 = (
    "2026-11-01 09:00:00",
    "2026-11-01 09:00:02",
    "2026-11-01 09:00:04",
    "2026-11-01 09:00:06",
)


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


def _task_with_channels(session, names: tuple[str, ...] = ("CH1",)) -> tuple[int, dict[str, int]]:
    """建任务 → 设备 → 监测对象 → 若干通道节点，返回 (task_id, 通道名 → 节点 id)。"""
    task = tasks.create_task(session, name="重算")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    nodes = {
        name: tree.create_node(session, task.id, "channel", name, parent_id=comp.id).id
        for name in names
    }
    return task.id, nodes


def _channel_file(session, task_id: int, channel_code: str, node_id: int) -> int:
    """建一个数据集 + 一个通道文件，返回通道行 id。"""
    ds = datasets.create_dataset(session, task_id, f"批次{node_id}", "/tmp/s4a", T1)
    return datasets.add_file(
        session, ds.id, f"/tmp/s4a/{channel_code}.dat", channel_code, FS, 128000
    ).id


def _value(node_id: int, code: str, value: float, **kwargs) -> NodeValue:
    return NodeValue(node_id=node_id, code=code, value=value, unit="mm/s", **kwargs)


def test_count_stale_counts_task_node_and_code(session):
    task_id, nodes = _task_with_channels(session, ("CH1", "CH2"))
    ch1 = _channel_file(session, task_id, "CH1", nodes["CH1"])
    ch2 = _channel_file(session, task_id, "CH2", nodes["CH2"])
    metric_store.upsert_node_values(
        session,
        ch1,
        T1,
        [_value(nodes["CH1"], "v_RMS", 1.0), _value(nodes["CH1"], "a_RMS", 0.5)],
    )
    metric_store.upsert_node_values(session, ch1, T2, [_value(nodes["CH1"], "v_RMS", 1.1)])
    metric_store.upsert_node_values(session, ch2, T1, [_value(nodes["CH2"], "v_RMS", 2.0)])
    metric_store.mark_stale(session, nodes["CH1"], "v_RMS")

    assert metric_store.count_stale(session, task_id) == 2
    assert metric_store.count_stale(session, task_id, node_id=nodes["CH1"]) == 2
    assert metric_store.count_stale(session, task_id, node_id=nodes["CH2"]) == 0
    assert metric_store.count_stale(session, task_id, node_id=nodes["CH1"], code="a_RMS") == 0


def test_count_stale_ignores_deleted_rows(session):
    task_id, nodes = _task_with_channels(session)
    ch1 = _channel_file(session, task_id, "CH1", nodes["CH1"])
    metric_store.upsert_node_values(session, ch1, T1, [_value(nodes["CH1"], "v_RMS", 1.0)])
    metric_store.mark_stale(session, nodes["CH1"], "v_RMS")
    model = session.query(MetricValue).one()
    model.deleted_at = model.computed_at
    session.commit()

    assert metric_store.count_stale(session, task_id) == 0


def test_replace_node_series_overwrites_same_points_and_retires_stale_ones(session):
    """同一时点覆盖为新值 + stale=0；不再产生的 stale 点软删；别家行不动。"""
    task_id, nodes = _task_with_channels(session, ("CH1", "CH2"))
    ch1 = _channel_file(session, task_id, "CH1", nodes["CH1"])
    ch2 = _channel_file(session, task_id, "CH2", nodes["CH2"])
    metric_store.upsert_node_values(
        session,
        ch1,
        T1,
        [
            _value(nodes["CH1"], "v_RMS", 1.0),
            _value(nodes["CH1"], "a_RMS", 0.5),
        ],
    )
    metric_store.upsert_node_values(session, ch1, T2, [_value(nodes["CH1"], "v_RMS", 1.1)])
    metric_store.upsert_node_values(session, ch2, T1, [_value(nodes["CH2"], "v_RMS", 2.0)])
    # 参数改了 → 该节点两指标都置 stale；别家节点也各自 stale（应互不影响）
    metric_store.mark_stale(session, nodes["CH1"], "v_RMS")
    metric_store.mark_stale(session, nodes["CH1"], "a_RMS")
    metric_store.mark_stale(session, nodes["CH2"], "v_RMS")

    written, retired = metric_store.replace_node_series(
        session,
        ch1,
        nodes["CH1"],
        "v_RMS",
        [
            (
                T1,
                _value(
                    nodes["CH1"],
                    "v_RMS",
                    7.0,
                    params_hash="abc123",
                    effective_params_json='{"band_high":500.0}',
                    rpm_used=1500.0,
                    rpm_source="rated",
                ),
            ),
            (T3, _value(nodes["CH1"], "v_RMS", 7.5, params_hash="abc123")),
        ],
    )

    assert (written, retired) == (2, 1)

    rows = {
        row.computed_at: row
        for row in metric_store.list_values(session, task_id, "CH1", "v_RMS", node_id=nodes["CH1"])
    }
    assert sorted(rows) == [T1, T3]
    assert rows[T1].value == pytest.approx(7.0)
    assert rows[T1].stale is False
    assert rows[T1].params_hash == "abc123"
    assert rows[T1].rpm_used == pytest.approx(1500.0)
    assert rows[T1].rpm_source == "rated"
    assert rows[T3].value == pytest.approx(7.5)
    assert rows[T3].stale is False

    # T2（旧窗长才有的点位）软删，不留在「待重算」里
    moment = datetime.strptime(T2, "%Y-%m-%d %H:%M:%S")
    retired_row = session.query(MetricValue).filter_by(channel_id=ch1, computed_at=moment).one()
    assert retired_row.deleted_at is not None

    # 同通道的另一个指标、另一个节点的行都没被动过
    still = {
        row.metric_code: row
        for row in metric_store.list_values(session, task_id, "CH1", "a_RMS", node_id=nodes["CH1"])
    }
    assert still["a_RMS"].stale is True
    assert still["a_RMS"].value == pytest.approx(0.5)
    other = metric_store.list_values(session, task_id, "CH2", "v_RMS", node_id=nodes["CH2"])
    assert other[0].stale is True


def test_replace_node_series_keeps_fresh_points_that_were_not_reproduced(session):
    """非 stale 的点位不因「本次没产生」被软删（只清待重算的旧点位）。"""
    task_id, nodes = _task_with_channels(session)
    ch1 = _channel_file(session, task_id, "CH1", nodes["CH1"])
    metric_store.upsert_node_values(
        session, ch1, T1, [_value(nodes["CH1"], "v_RMS", 1.0, params_hash="old")]
    )
    metric_store.mark_stale(session, nodes["CH1"], "v_RMS")
    # 标记之后又导入的新点（stale=0），本次重算的点位里没有它
    metric_store.upsert_node_values(session, ch1, T4, [_value(nodes["CH1"], "v_RMS", 2.0)])

    written, retired = metric_store.replace_node_series(
        session, ch1, nodes["CH1"], "v_RMS", [(T1, _value(nodes["CH1"], "v_RMS", 3.0))]
    )

    assert (written, retired) == (1, 0)
    assert len(metric_store.list_values(session, task_id, "CH1", "v_RMS")) == 2
