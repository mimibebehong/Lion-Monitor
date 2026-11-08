"""监测对象健康度（2026-11-04）。

口径（现场原话）：**健康度用监测对象最新的 5 个指标点的 mean(指标/阈值) 代表**。

实现约定（写死在测试里，防止以后被改口径）：

- 取该监测对象**子树内所有指标节点**的点，按 ``computed_at`` 倒序，取**最新 5 个有效点**；
- 每个点算 ``指标值 / 该指标节点的生效阈值``；
- 阈值优先取 ``alert``（严重线），没设 alert 才用 ``warn``；**1.0 = 踩到严重线**；
- ``stale``（旧参数算的）点不参与；阈值与值都没法算的点跳过；
- 方向为「越低越差」的指标反算 ``阈值 / 值``，保持「越大越差」的统一语义；
- 一个可算的点都没有 → 健康度为 ``None``（界面显示「—」），不许假装 0。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lion_monitor.features import overview
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "hs.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


def _build(session, tmp_path, *, channel_values, channel_code="CH1"):
    """建 任务→设备→对象→通道→指标 树，并按时间点写指标值。

    ``channel_values``: [(stamp, [(metric_name, value, stale), ...]), ...]
    """
    task = tasks.create_task(session, name="健康度")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", channel_code, parent_id=comp.id)
    metric_nodes = {}
    for name in ("v_RMS", "a_Peak"):
        metric_nodes[name] = tree.create_node(
            session, task.id, "metric", name, parent_id=channel.id, attrs={"code": name}
        )
    limits.set_limit(session, task.id, channel.id, "v_RMS", warn=2.0, alert=4.0)
    limits.set_limit(session, task.id, channel.id, "a_Peak", warn=1.0, alert=2.0)

    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "x.dat", channel_code, 32000.0, 64000)

    for stamp, rows in channel_values:
        metric_store.upsert_node_values(
            session,
            file_row.id,
            stamp,
            [
                NodeValue(
                    node_id=channel.id,
                    code=name,
                    value=value,
                    unit="mm/s" if name == "v_RMS" else "g",
                    stale=stale,
                )
                for name, value, stale in rows
            ],
        )
    return {"task": task, "comp": comp.id, "channel": channel.id, "metrics": metric_nodes}


def test_health_score_is_mean_of_value_over_threshold(session, tmp_path):
    """v_RMS 2.0/4.0 = 0.5，a_Peak 1.0/2.0 = 0.5 → 健康度 0.5（两个点）。"""
    built = _build(
        session,
        tmp_path,
        channel_values=[("2026-11-01 10:00:00", [("v_RMS", 2.0, False), ("a_Peak", 1.0, False)])],
    )

    scores = overview.object_health_scores(session, built["task"].id)

    score, points = scores[built["comp"]]
    assert points == 2
    assert score == pytest.approx(0.5)


def test_health_score_uses_only_latest_five_points(session, tmp_path):
    """6 个点里只算最新 5 个：最老那个 4.0（比值 1.0）不该拉高健康度。"""
    values = [
        (f"2026-11-01 10:0{i}:00", [("v_RMS", 4.0 if i == 0 else 1.0, False)]) for i in range(6)
    ]
    built = _build(session, tmp_path, channel_values=values)

    score, points = overview.object_health_scores(session, built["task"].id)[built["comp"]]

    assert points == 5
    assert score == pytest.approx(0.25)


def test_health_score_skips_stale_points(session, tmp_path):
    """stale 点（旧参数算的）不参与健康度。"""
    built = _build(
        session,
        tmp_path,
        channel_values=[
            ("2026-11-01 10:00:00", [("v_RMS", 3.6, True)]),  # 比值 0.9，但已 stale
            ("2026-11-01 10:01:00", [("v_RMS", 2.0, False)]),  # 0.5
        ],
    )

    score, points = overview.object_health_scores(session, built["task"].id)[built["comp"]]

    assert points == 1
    assert score == pytest.approx(0.5)


def test_health_score_prefers_alert_then_warn(session, tmp_path):
    """阈值优先 alert；只有 warn 时用 warn；都没设 → 跳过。"""
    task = tasks.create_task(session, name="阈值优先级")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    v = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch.id, attrs={"code": "v_RMS"}
    )
    a = tree.create_node(
        session, task.id, "metric", "a_Peak", parent_id=ch.id, attrs={"code": "a_Peak"}
    )
    u = tree.create_node(
        session, task.id, "metric", "a_RMS", parent_id=ch.id, attrs={"code": "a_RMS"}
    )
    limits.set_limit(session, task.id, ch.id, "v_RMS", warn=1.0, alert=4.0)  # 用 alert → 2.0/4.0
    # a_Peak / a_RMS 都不设阈值 → 该点跳过

    folder = tmp_path / "d2"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    f = datasets.add_file(session, ds.id, folder / "x.dat", "CH1", 32000.0, 64000)
    metric_store.upsert_node_values(
        session,
        f.id,
        "2026-11-01 10:00:00",
        [
            NodeValue(node_id=ch.id, code="v_RMS", value=2.0, unit="mm/s"),
            NodeValue(node_id=ch.id, code="a_Peak", value=99.0, unit="g"),
            NodeValue(node_id=ch.id, code="a_RMS", value=5.0, unit="g"),
        ],
    )

    score, points = overview.object_health_scores(session, task.id)[comp.id]

    assert points == 1
    assert score == pytest.approx(0.5)
    assert v is not None and a is not None and u is not None


def test_health_score_none_when_no_points(session, tmp_path):
    """没有点 → None（界面显示「—」），不许当 0。"""
    built = _build(session, tmp_path, channel_values=[])

    score, points = overview.object_health_scores(session, built["task"].id)[built["comp"]]

    assert score is None
    assert points == 0


def test_health_score_inverted_direction_flips_ratio(session, tmp_path):
    """「越低越差」的指标反算 阈值/值，保持越大越差 —— 结果仍受 [0,1] 上限约束。

    这里 8.0/4.0 = 2.0，按2026-11-04「健康度最大不超过 1」应显示 1.00。
    """
    task = tasks.create_task(session, name="方向")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "metric", "kept", parent_id=ch.id, attrs={"code": "kept"})
    limits.set_limit(session, task.id, ch.id, "kept", warn=10.0, alert=8.0, direction="<")

    folder = tmp_path / "d3"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    f = datasets.add_file(session, ds.id, folder / "x.dat", "CH1", 32000.0, 64000)
    metric_store.upsert_node_values(
        session, f.id, "2026-11-01 10:00:00", [NodeValue(node_id=ch.id, code="kept", value=4.0)]
    )

    score, points = overview.object_health_scores(session, task.id)[comp.id]

    assert points == 1
    assert score == pytest.approx(1.0)  # 8.0/4.0=2.0，按上限 clamp 到 1.0


def _single_point_score(session, task, comp, ch, code, value, warn, alert):
    limits.set_limit(session, task.id, ch.id, code, warn=warn, alert=alert)
    folder = Path("/tmp/_hs_clamp")
    folder.mkdir(parents=True, exist_ok=True)
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    f = datasets.add_file(session, ds.id, folder / f"{code}.dat", "CH1", 32000.0, 64000)
    metric_store.upsert_node_values(
        session, f.id, "2026-11-01 10:00:00", [NodeValue(node_id=ch.id, code=code, value=value)]
    )
    return overview.object_health_scores(session, task.id)[comp.id]


def test_health_score_capped_at_one(session):
    """2026-11-04：健康度最大不超过 1 —— 值远超阈值也只显示 1.00。"""
    task = tasks.create_task(session, name="上限")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "metric", "v_RMS", parent_id=ch.id, attrs={"code": "v_RMS"})

    score, points = _single_point_score(session, task, comp, ch, "v_RMS", 100.0, 1.0, 2.0)

    assert points == 1
    assert score == 1.0


def test_health_score_floor_at_zero(session):
    """2026-11-04：健康度最小不低于 0 —— 负值不再冒出负数。"""
    task = tasks.create_task(session, name="下限")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "设备", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "对象", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    tree.create_node(session, task.id, "metric", "v_RMS", parent_id=ch.id, attrs={"code": "v_RMS"})

    score, points = _single_point_score(session, task, comp, ch, "v_RMS", -5.0, 1.0, 2.0)

    assert points == 1
    assert score == 0.0
