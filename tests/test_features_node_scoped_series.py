"""P2 / S1：同一通道号挂多个监测对象时，健康评级 / 超限记录 / 逐点取数各按自己的节点。

口径（Q1 拍板允许同通道多对象 + 方案 A「值按节点存」）：

- 指标值按**通道节点**落库（``metric_value.node_id`` 是健康树通道节点 id）
- 健康评级 / 超限记录只读本节点那几行，不把同通道其它节点的点混进来（不再串值）
- 每个指标节点按**自己**的生效阈值判级
- 历史无归属行（``node_id IS NULL``）：该通道名在本任务下只有一个通道节点时一并算，
  同名通道有多个节点时不带（无法归属，带进来就又是串值）
"""

from __future__ import annotations

import pytest

from lion_monitor.features import health, overview, series
from lion_monitor.features.metrics import store_metrics
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

STAMPS = ("2026-11-01 10:00:00", "2026-11-01 10:01:00", "2026-11-01 10:02:00")


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def two_objects(session, tmp_path):
    """同一通道号 CH1 挂在两个监测对象下：每个对象各 3 个指标点（值不同）。"""
    task = tasks.create_task(session, name="同通道多对象")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)

    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)

    out: dict = {"task": task, "device": device.id, "file": file_row.id}
    for name, values in (("轴承A", (3.0, 3.5, 4.0)), ("轴承B", (1.0, 1.2, 1.4))):
        comp = tree.create_node(session, task.id, "component", name, parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric = tree.create_node(
            session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
        )
        out[name] = {"comp": comp.id, "channel": channel.id, "metric": metric.id, "values": values}

    # 同一通道文件里，两个节点各存一行：一次写入带上两个节点（同整点先清后写）
    for idx, stamp in enumerate(STAMPS):
        metric_store.upsert_node_values(
            session,
            file_row.id,
            stamp,
            [
                NodeValue(
                    node_id=out["轴承A"]["channel"],
                    code="v_RMS",
                    value=out["轴承A"]["values"][idx],
                    unit="mm/s",
                ),
                NodeValue(
                    node_id=out["轴承B"]["channel"],
                    code="v_RMS",
                    value=out["轴承B"]["values"][idx],
                    unit="mm/s",
                ),
            ],
        )
    # 两个对象的阈值各不相同（A：2.2 / 3.8；B：0.5 / 0.9）
    limits.set_limit(session, task.id, out["轴承A"]["metric"], "v_RMS", 2.2, 3.8)
    limits.set_limit(session, task.id, out["轴承B"]["metric"], "v_RMS", 0.5, 0.9)
    session.commit()
    return out


@pytest.fixture()
def single_object(session, tmp_path):
    """一个监测对象 + CH1 一个通道节点（老数据的归属无歧义）。"""
    task = tasks.create_task(session, name="单对象")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
    )
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)
    return {
        "task": task,
        "comp": comp.id,
        "channel": channel.id,
        "metric": metric.id,
        "file": file_row.id,
    }


# ---------------------------------------------------------------- 逐点取数（features.series）


def test_metric_series_is_scoped_to_its_own_channel_node(session, two_objects):
    found = {
        item.metric_node.id: item for item in series.metric_series(session, two_objects["task"].id)
    }

    a = found[two_objects["轴承A"]["metric"]]
    b = found[two_objects["轴承B"]["metric"]]

    assert a.channel_node_id == two_objects["轴承A"]["channel"]
    assert b.channel_node_id == two_objects["轴承B"]["channel"]
    assert [row.value for row in a.values] == [pytest.approx(v) for v in (3.0, 3.5, 4.0)]
    assert [row.value for row in b.values] == [pytest.approx(v) for v in (1.0, 1.2, 1.4)]


def test_legacy_rows_are_read_when_channel_has_one_node(session, single_object):
    """老数据（node_id 为空）在通道只有一个节点时照常读出来，不丢点。"""
    store_metrics(session, single_object["file"], {"v_RMS": 2.5}, STAMPS[0])
    metric_store.upsert_node_values(
        session,
        single_object["file"],
        STAMPS[1],
        [NodeValue(node_id=single_object["channel"], code="v_RMS", value=3.1, unit="mm/s")],
    )

    item = series.metric_series(session, single_object["task"].id)[0]

    assert [row.value for row in item.values] == [pytest.approx(2.5), pytest.approx(3.1)]


def test_legacy_rows_are_not_duplicated_across_two_nodes(session, two_objects):
    """同名通道有两个节点：无归属的历史行不能塞给任何一方（否则又串值）。"""
    store_metrics(session, two_objects["file"], {"v_RMS": 99.0}, "2026-11-01 09:30:00")

    found = {
        item.metric_node.id: item for item in series.metric_series(session, two_objects["task"].id)
    }

    assert len(found[two_objects["轴承A"]["metric"]].values) == 3
    assert len(found[two_objects["轴承B"]["metric"]].values) == 3
    assert all(row.node_id is not None for item in found.values() for row in item.values)


# ---------------------------------------------------------------- 健康评级


def test_health_uses_only_its_own_node_rows(session, two_objects):
    result = health.node_health(session, two_objects["task"].id)

    a_metric = result[two_objects["轴承A"]["metric"]]
    b_metric = result[two_objects["轴承B"]["metric"]]

    assert a_metric.latest_value == pytest.approx(4.0)
    assert a_metric.over_count == 3
    assert b_metric.latest_value == pytest.approx(1.4)
    assert b_metric.over_count == 3


def test_health_upward_summary_does_not_double_count(session, two_objects):
    result = health.node_health(session, two_objects["task"].id)

    assert result[two_objects["轴承A"]["channel"]].over_count == 3
    assert result[two_objects["轴承B"]["channel"]].over_count == 3
    assert result[two_objects["轴承A"]["comp"]].over_count == 3
    assert result[two_objects["device"]].over_count == 6  # 两个对象各 3 条，不是 12


def test_health_grade_uses_the_nodes_own_threshold(session, two_objects):
    """同值不同阈值：A 判严重、B 判正常 —— 各按自己的生效阈值。"""
    for stamp in STAMPS:
        metric_store.upsert_node_values(
            session,
            two_objects["file"],
            stamp,
            [
                NodeValue(
                    node_id=two_objects["轴承A"]["channel"], code="v_RMS", value=4.0, unit="mm/s"
                ),
                NodeValue(
                    node_id=two_objects["轴承B"]["channel"], code="v_RMS", value=4.0, unit="mm/s"
                ),
            ],
        )
    # B 的阈值抬到 5 / 9：同样的 4.0 在 B 下是正常
    limits.set_limit(
        session, two_objects["task"].id, two_objects["轴承B"]["metric"], "v_RMS", 5.0, 9.0
    )
    session.commit()

    result = health.node_health(session, two_objects["task"].id)

    assert result[two_objects["轴承A"]["metric"]].state == "alert"
    assert result[two_objects["轴承B"]["metric"]].state == "ok"


def test_health_counts_legacy_rows_only_for_sole_owner(session, single_object):
    store_metrics(session, single_object["file"], {"v_RMS": 9.0}, STAMPS[0])
    limits.set_limit(session, single_object["task"].id, single_object["metric"], "v_RMS", 2.2, 3.8)
    session.commit()

    result = health.node_health(session, single_object["task"].id)

    assert result[single_object["metric"]].latest_value == pytest.approx(9.0)
    assert result[single_object["metric"]].over_count == 1


# ---------------------------------------------------------------- 工作台超限记录


def test_over_records_counted_per_channel_node(session, two_objects):
    records = overview.over_records(session, two_objects["task"].id)

    assert len(records) == 6  # 每个对象 3 条，不是「按通道号去重成 3 条」
    per_node = {}
    for record in records:
        per_node[record.node_id] = per_node.get(record.node_id, 0) + 1
    assert per_node == {two_objects["轴承A"]["channel"]: 3, two_objects["轴承B"]["channel"]: 3}


def test_overview_kpi_counts_each_object_once(session, two_objects):
    summary = overview.task_overview(session, two_objects["task"].id)

    assert summary.over_count == 6
    # A：3.0/3.5 警告 + 4.0 严重；B：1.0/1.2/1.4 都越过 alert 0.9 → 严重 1+3 条
    assert summary.over_level_count("alert") == 4
    assert summary.health_counts["alert"] == 2  # 健康分布按指标节点各自判级
