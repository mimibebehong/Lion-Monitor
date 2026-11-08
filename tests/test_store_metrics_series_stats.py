"""指标序列 SQL 聚合（P3 / S2）：计数 / 最新值 / 越限计数在库里算，不把整串值拉回 Python。

口径必须与逐点判级（``limits_rules.grade`` + ``features.health.point_level``）一致：

- ``count`` = 该序列的有效行数（按节点取数口径，见 P2 / S1）
- ``warn_count`` / ``alert_count`` = 越过 warn / alert 的点数（先判 alert，命中即 alert）
- ``latest`` = 时间最新的一行（同刻取 id 更大的），给健康树判级用
- 没给阈值 → 两个越限计数都是 0（不判级，也不许瞎猜）
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from _sql_count import counted_statements

from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import MetricValue, NodeValue

STAMP = "2026-11-01 10:00:00"
LIMIT = (2.8, 4.5, ">", "指标级")


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def built(session, tmp_path):
    """任务 → 设备 → 对象 → CH1 通道节点 + v_RMS 指标节点，返回关键 id。"""
    task = tasks.create_task(session, name="聚合任务")
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
        "channel": channel.id,
        "metric": metric.id,
        "file": file_row.id,
        "dataset": ds.id,
        "folder": folder,
    }


def _add_points(session, built, values, *, node_id=None, code="v_RMS", start=STAMP):
    """直接落点（绕开写入侧，造数快）：一分钟一个点。"""
    base = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    for index, value in enumerate(values):
        session.add(
            MetricValue(
                channel_id=built["file"],
                node_id=built["channel"] if node_id is None else node_id,
                code=code,
                value=value,
                unit="mm/s",
                level="unset",
                computed_at=base + timedelta(minutes=index),
            )
        )
    session.commit()


# ---------------------------------------------------------------- 计数 / 最新值 / 越限数


def test_stats_count_latest_and_over_levels(session, built):
    _add_points(session, built, [0.5, 1.2, 2.9, 5.0])

    stats = metric_store.series_stats(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=LIMIT,
    )

    assert stats.count == 4
    assert (stats.warn_count, stats.alert_count) == (1, 1)
    assert stats.over_count == 2
    assert stats.latest is not None
    assert stats.latest.value == pytest.approx(5.0)
    assert stats.latest.computed_at == "2026-11-01 10:03:00"
    assert stats.latest.unit == "mm/s"


def test_stats_latest_is_the_newest_row_when_times_tie(session, built):
    """同刻两行（两个批次各登记同通道文件，库里唯一约束允许）：取 id 更大的那行当最新。"""
    moment = datetime.strptime(STAMP, "%Y-%m-%d %H:%M:%S")
    other = datasets.create_dataset(
        session, built["task"].id, "批次2", built["folder"], "2026-11-01 11:00:00"
    )
    second = datasets.add_file(
        session, other.id, built["folder"] / "CH1_again.dat", "CH1", 32000.0, 64000
    )
    for file_id, value in ((built["file"], 1.0), (second.id, 3.3)):
        session.add(
            MetricValue(
                channel_id=file_id,
                node_id=built["channel"],
                code="v_RMS",
                value=value,
                unit="mm/s",
                level="unset",
                computed_at=moment,
            )
        )
    session.commit()

    stats = metric_store.series_stats(
        session, built["task"].id, "CH1", "v_RMS", node_id=built["channel"]
    )

    assert stats.count == 2
    assert stats.latest is not None
    assert stats.latest.value == pytest.approx(3.3)  # 同刻取 id 更大的那行


def test_stats_counts_lower_direction_from_the_alert_side(session, built):
    """下限阈值（越小越差）：先判 alert，再判 warn，与 grade 一致。"""
    _add_points(session, built, [5.0, 2.5, 1.5, 0.9])

    stats = metric_store.series_stats(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=(2.8, 1.5, "<", "指标级"),
    )

    assert (stats.warn_count, stats.alert_count) == (1, 2)


def test_stats_without_limit_reports_no_over_counts(session, built):
    _add_points(session, built, [9.9, 9.9])

    stats = metric_store.series_stats(session, built["task"].id, "CH1", "v_RMS")

    assert stats.count == 2
    assert (stats.warn_count, stats.alert_count) == (0, 0)
    assert stats.latest is not None
    assert stats.latest.value == pytest.approx(9.9)


def test_stats_keeps_node_scope(session, built):
    """同通道号另一个节点的行不算进来（P2 / S1 的取数口径）。"""
    other_node = built["channel"] + 999
    _add_points(session, built, [0.5, 6.0], node_id=built["channel"])
    _add_points(session, built, [9.0], node_id=other_node, start="2026-11-01 11:00:00")

    stats = metric_store.series_stats(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=LIMIT,
    )

    assert stats.count == 2
    assert stats.latest is not None
    assert stats.latest.value == pytest.approx(6.0)


def test_stats_can_include_unattributed_legacy_rows(session, built):
    metric_store.upsert_values(
        session, built["file"], {"v_RMS": 4.4}, {"v_RMS": "mm/s"}, "2026-11-01 12:00:00"
    )
    _add_points(session, built, [1.0])

    strict = metric_store.series_stats(
        session, built["task"].id, "CH1", "v_RMS", node_id=built["channel"], limit=LIMIT
    )
    loose = metric_store.series_stats(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        include_unattributed=True,
        limit=LIMIT,
    )

    # 口径：warn_count 只算「越过 warn 未到 alert」；4.4 对 2.8/4.5 是 warn，不是 alert
    assert (strict.count, strict.warn_count, strict.alert_count) == (1, 0, 0)
    assert (loose.count, loose.warn_count, loose.alert_count) == (2, 1, 0)


# ---------------------------------------------------------------- 真的走 SQL（不拉全量）


def test_stats_runs_aggregate_statement_instead_of_pulling_every_row(session, built):
    _add_points(session, built, [1.0] * 300)

    with counted_statements(session.get_bind()) as seen:
        stats = metric_store.series_stats(
            session,
            built["task"].id,
            "CH1",
            "v_RMS",
            node_id=built["channel"],
            limit=LIMIT,
        )

    assert stats.count == 300
    assert len(seen) <= 2  # 计数 + 最新值各一条
    assert any("count(" in text.lower() for text in seen)


def test_latest_row_comes_from_aggregate_plus_lookup_not_a_window_sort(session, built):
    """最新值口径：聚合里顺带算出每组最新时刻，再按 (节点, 指标, 时刻) 回取那几行。

    P3 / S3：24 万行时 ``row_number()`` 窗口要把整表排序（实测 797 ms），
    改成「一条聚合（自带 max(computed_at)）+ 一条按键回取」后降到 204 ms。
    """
    _add_points(session, built, [1.0, 2.0, 3.0])

    with counted_statements(session.get_bind()) as seen:
        stats = metric_store.series_stats(
            session, built["task"].id, "CH1", "v_RMS", node_id=built["channel"]
        )

    assert stats.latest is not None and stats.latest.value == pytest.approx(3.0)
    assert len(seen) <= 2
    assert any("max(" in text.lower() for text in seen)  # 聚合顺带算最新时刻
    assert not any("row_number" in text.lower() for text in seen)  # 不再整表窗口排序


def test_over_values_returns_only_over_limit_rows_in_time_order(session, built):
    _add_points(session, built, [0.5, 3.1, 1.0, 5.2, 2.9])

    rows = metric_store.over_values(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=LIMIT,
    )

    assert [row.value for row in rows] == [
        pytest.approx(3.1),
        pytest.approx(5.2),
        pytest.approx(2.9),
    ]
    assert [row.computed_at for row in rows] == [
        "2026-11-01 10:01:00",
        "2026-11-01 10:03:00",
        "2026-11-01 10:04:00",
    ]


def test_over_values_needs_a_limit(session, built):
    _add_points(session, built, [9.9])

    with pytest.raises(ValueError):
        metric_store.over_values(session, built["task"].id, "CH1", "v_RMS", limit=None)


def test_over_values_skips_non_finite_values(session, built):
    """±Inf 不参与比较（与 limits_rules.grade 一致）：坏值不能算成越限。"""
    _add_points(session, built, [3.1, float("inf"), float("-inf"), 5.2])

    rows = metric_store.over_values(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=LIMIT,
    )
    stats = metric_store.series_stats(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        limit=LIMIT,
    )

    assert [row.value for row in rows] == [pytest.approx(3.1), pytest.approx(5.2)]
    assert (stats.warn_count, stats.alert_count) == (1, 1)


# ---------------------------------------------------------------- 时间窗（趋势页默认 30 天）


def test_list_values_honours_since_window(session, built):
    _add_points(session, built, [1.0, 2.0, 3.0])

    rows = metric_store.list_values(
        session,
        built["task"].id,
        "CH1",
        "v_RMS",
        node_id=built["channel"],
        since="2026-11-01 10:01:00",
    )

    assert [row.value for row in rows] == [pytest.approx(2.0), pytest.approx(3.0)]


def test_upsert_node_values_still_round_trips_value_rows(session, built):
    """聚合改造不能动写入侧：一轮写读结果与改造前一致。"""
    metric_store.upsert_node_values(
        session,
        built["file"],
        STAMP,
        [NodeValue(node_id=built["channel"], code="v_RMS", value=2.5, unit="mm/s")],
    )

    rows = metric_store.list_values(
        session, built["task"].id, "CH1", "v_RMS", node_id=built["channel"]
    )

    assert [(row.value, row.unit, row.node_id) for row in rows] == [
        (pytest.approx(2.5), "mm/s", built["channel"])
    ]
