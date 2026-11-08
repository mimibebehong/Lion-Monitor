"""健康树评级改走 SQL 聚合（P3 / S2）：最新值与超限条数在库里算，且不再按节点逐个查祖先链。

口径不变（方案 9.10 健康评级 + P2 / S1 按节点取数）：

- 指标节点：最新一点 vs 继承阈值 → ok / warn / alert / invalid / unset；
  ``over_count`` = 越过 warn 的点数（含 alert）
- 上层节点：状态取子树最差，``over_count`` 为子树累计
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from _sql_count import counted_statements

from lion_monitor.features import health, series
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store.models import MetricValue

START = "2026-11-01 10:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "h.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def built(session, tmp_path):
    """设备 → 两个对象（各 CH1/CH2，各挂 v_RMS + a_Peak），4 条指标序列。"""
    task = tasks.create_task(session, name="健康任务")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")

    out: dict = {"task": task, "device": device.id, "files": {}}
    for comp_name, codes in (("轴承A", ("CH1", "CH2")), ("轴承B", ("CH3", "CH4"))):
        comp = tree.create_node(session, task.id, "component", comp_name, parent_id=device.id)
        out[comp_name] = {"comp": comp.id, "channels": {}, "metrics": {}}
        for code_ch in codes:
            channel = tree.create_node(session, task.id, "channel", code_ch, parent_id=comp.id)
            out[comp_name]["channels"][code_ch] = channel.id
            out[comp_name]["metrics"][code_ch] = {}
            for code in ("v_RMS", "a_Peak"):
                metric = tree.create_node(
                    session,
                    task.id,
                    "metric",
                    code,
                    parent_id=channel.id,
                    attrs={"code": code},
                )
                out[comp_name]["metrics"][code_ch][code] = metric.id
            file_row = datasets.add_file(
                session, ds.id, folder / f"{code_ch}_Task1.dat", code_ch, 32000.0, 64000
            )
            out["files"][code_ch] = file_row.id
    return out


def _add_points(session, file_id, node_id, code, values, *, start=START):
    base = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    for index, value in enumerate(values):
        session.add(
            MetricValue(
                channel_id=file_id,
                node_id=node_id,
                code=code,
                value=value,
                unit="mm/s",
                level="unset",
                computed_at=base + timedelta(seconds=index),
            )
        )
    session.commit()


def test_metric_node_uses_sql_latest_and_over_count(session, built):
    ch1 = built["轴承A"]["channels"]["CH1"]
    values = [0.5, 1.0, 3.0, 5.0] + [1.0] * 6
    _add_points(session, built["files"]["CH1"], ch1, "v_RMS", values)
    limits.set_limit(
        session, built["task"].id, built["轴承A"]["metrics"]["CH1"]["v_RMS"], "v_RMS", 2.8, 4.5
    )

    result = health.node_health(session, built["task"].id)

    item = result[built["轴承A"]["metrics"]["CH1"]["v_RMS"]]
    assert item.latest_value == pytest.approx(1.0)
    assert item.state == "ok"
    assert item.over_count == 2  # 3.0 警告 + 5.0 严重
    assert item.latest_at == "2026-11-01 10:00:09"


def test_upper_nodes_aggregate_subtree_from_metric_rows(session, built):
    _add_points(session, built["files"]["CH1"], built["轴承A"]["channels"]["CH1"], "v_RMS", [3.0])
    _add_points(session, built["files"]["CH2"], built["轴承A"]["channels"]["CH2"], "a_Peak", [6.0])
    limits.set_limit(
        session, built["task"].id, built["轴承A"]["metrics"]["CH1"]["v_RMS"], "v_RMS", 2.8, 4.5
    )
    limits.set_limit(
        session, built["task"].id, built["轴承A"]["metrics"]["CH2"]["a_Peak"], "a_Peak", 1.5, 4.0
    )

    result = health.node_health(session, built["task"].id)

    assert result[built["轴承A"]["comp"]].over_count == 2
    assert result[built["轴承A"]["comp"]].state == "alert"
    assert result[built["device"]].over_count == 2
    assert result[built["device"]].state == "alert"


def test_health_does_not_walk_the_ancestor_chain_per_node(session, built, monkeypatch):
    """N+1 回归：path_nodes 在健康评级里一次都不能被调（原来每个节点 2 次查询）。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("node_health 不该再按节点查祖先链（path_nodes）")

    monkeypatch.setattr(tree, "path_nodes", _boom)

    result = health.node_health(session, built["task"].id)

    assert len(result) == len(tree.list_nodes(session, built["task"].id))


def test_health_does_not_pull_every_value_row(session, built, monkeypatch):
    """N+1 回归：不再调 ``series.metric_series``（那会把整串点拉成 ValueRow）。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("node_health 不该再走 metric_series 全量拉取")

    monkeypatch.setattr(series, "metric_series", _boom)

    result = health.node_health(session, built["task"].id)

    assert set(result) == {node.id for node in tree.list_nodes(session, built["task"].id)}


def test_health_statement_count_is_bounded(session, built):
    """4 条序列 × 100 点：语句数固定（不随点数增长）。"""
    for comp_name, codes in (("轴承A", ("CH1", "CH2")), ("轴承B", ("CH3", "CH4"))):
        for code_ch in codes:
            for code in ("v_RMS", "a_Peak"):
                _add_points(
                    session,
                    built["files"][code_ch],
                    built[comp_name]["channels"][code_ch],
                    code,
                    [1.0] * 100,
                )

    with counted_statements(session.get_bind()) as seen:
        health.node_health(session, built["task"].id)

    assert len(seen) <= 20, f"语句数 {len(seen)} 条，疑似按点/按节点查询"


def test_health_grades_non_finite_latest_as_invalid(session, built):
    """±Inf 落库（老数据）不参与比较：状态「数据异常」，也不计进超限条数。"""
    ch1 = built["轴承A"]["channels"]["CH1"]
    _add_points(session, built["files"]["CH1"], ch1, "v_RMS", [1.0, float("inf")])
    limits.set_limit(
        session, built["task"].id, built["轴承A"]["metrics"]["CH1"]["v_RMS"], "v_RMS", 2.8, 4.5
    )

    result = health.node_health(session, built["task"].id)

    item = result[built["轴承A"]["metrics"]["CH1"]["v_RMS"]]
    assert item.state == "invalid"
    assert item.over_count == 0
