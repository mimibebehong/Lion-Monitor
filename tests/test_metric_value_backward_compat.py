"""向后兼容回归：历史 ``metric_value`` 行的 ``node_id`` 为空时，查询 / 趋势 / 报告不能报错。

方案 A 之前落库的值没有节点归属（``node_id IS NULL``），新代码必须继续读得出来：
- ``store.metrics.list_values`` / ``trend_series`` 不因 node_id 为空报错或漏行
- 健康评级（``features.health``）、工作台概览（``features.overview``）照常算
- 老库（唯一约束是旧版）跑完迁移后，历史行原样还在
"""

from __future__ import annotations

import pytest

from lion_monitor.features import health, overview
from lion_monitor.features.metrics import store_metrics
from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

STAMP = "2026-10-30 09:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def task(session):
    return tasks.create_task(session, name="兼容任务")


@pytest.fixture()
def channel(session, task, tmp_path):
    """任务 → 设备 → 监测对象 → 通道节点 + 一个指标节点，返回 (通道节点 id, 通道文件 id)。"""
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=node.id, attrs={"code": "v_RMS"}
    )
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-10-30 08:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)
    return node.id, file_row.id


def _legacy_value(session, file_id: int, value: float = 2.5, stamp: str = STAMP) -> None:
    """模拟改造前写入的行：没有 node_id（走老 upsert_values 签名）。"""
    store_metrics(session, file_id, {"v_RMS": value}, stamp)


def test_legacy_row_reads_back_with_empty_node_id(session, task, channel):
    _node_id, file_id = channel
    _legacy_value(session, file_id)

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS")

    assert len(rows) == 1
    assert rows[0].node_id is None
    assert rows[0].value == pytest.approx(2.5)


def test_mixed_legacy_and_node_rows_do_not_break_queries(session, task, channel):
    node_id, file_id = channel
    _legacy_value(session, file_id)
    metric_store.upsert_node_values(
        session,
        file_id,
        "2026-10-30 10:00:00",
        [NodeValue(node_id=node_id, code="v_RMS", value=3.1, unit="mm/s")],
    )

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS")
    series = metric_store.trend_series(session, task.id, "CH1", "v_RMS")

    assert [row.node_id for row in rows] == [None, node_id]
    assert [value for _stamp, value in series] == [pytest.approx(2.5), pytest.approx(3.1)]


def test_node_filter_skips_legacy_rows(session, task, channel):
    node_id, file_id = channel
    _legacy_value(session, file_id)
    metric_store.upsert_node_values(
        session,
        file_id,
        "2026-10-30 10:00:00",
        [NodeValue(node_id=node_id, code="v_RMS", value=3.1, unit="mm/s")],
    )

    rows = metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=node_id)

    assert [row.value for row in rows] == [pytest.approx(3.1)]


def test_health_rating_works_with_legacy_rows(session, task, channel):
    _node_id, file_id = channel
    _legacy_value(session, file_id, 9.9)

    result = health.node_health(session, task.id)

    metric_node = next(
        node for node in tree.list_nodes(session, task.id) if node.node_type == "metric"
    )
    assert result[metric_node.id].latest_value == pytest.approx(9.9)
    assert result[metric_node.id].state == "unset"  # 没设阈值 → unset，但不报错


def test_report_overview_works_with_legacy_rows(session, task, channel):
    _node_id, file_id = channel
    _legacy_value(session, file_id, 9.9)
    metric_store.upsert_node_values(
        session,
        file_id,
        "2026-10-31 10:00:00",
        [NodeValue(node_id=channel[0], code="v_RMS", value=1.5, unit="mm/s")],
    )

    records = overview.over_records(session, task.id)  # 没有阈值 → 空，但不得抛错
    counts = overview.health_counts(session, task.id)
    steps = overview.step_states(session, task.id)

    assert records == []
    assert counts["unset"] >= 1
    assert len(steps) == len(overview.STEPS)


def test_legacy_db_rows_survive_migration_and_read(session, task, tmp_path):
    """老库（旧唯一约束 + 无 node_id）跑完 init_db：历史行还在，查询照常。"""
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE metric_value ("
            " id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, code VARCHAR(32) NOT NULL,"
            " value FLOAT NOT NULL, unit VARCHAR(16), level VARCHAR(16),"
            " computed_at DATETIME NOT NULL, deleted_at DATETIME,"
            " CONSTRAINT uq_metric_value_point UNIQUE (channel_id, code, computed_at))"
        )
    store_db.init_db(engine)

    with store_db.make_session_factory(engine)() as session:
        row = tasks.create_task(session, name="老库任务")
        root = tree.root_node(session, row.id)
        device = tree.create_node(session, row.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, row.id, "component", "轴承A", parent_id=device.id)
        tree.create_node(session, row.id, "channel", "CH1", parent_id=comp.id)
        folder = tmp_path / "old"
        folder.mkdir()
        ds = datasets.create_dataset(session, row.id, "老批次", folder, "2026-10-01 08:00:00")
        file_row = datasets.add_file(
            session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000
        )
        # store 写函数只 flush（P1 / B2）；下面要用另一条连接写「老口径」行，先提交本会话
        session.commit()
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO metric_value (channel_id, code, value, unit, level, computed_at)"
                " VALUES (?, 'v_RMS', 4.2, 'mm/s', 'unset', '2026-10-01 10:00:00')",
                (file_row.id,),
            )

        rows = metric_store.list_values(session, row.id, "CH1", "v_RMS")
        assert [r.value for r in rows] == [pytest.approx(4.2)]
        assert rows[0].node_id is None
        assert metric_store.trend_series(session, row.id, "CH1", "v_RMS") == [
            ("2026-10-01 10:00:00", pytest.approx(4.2))
        ]
