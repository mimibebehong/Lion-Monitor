"""批次 B-3（复查 v2 问题 4）：健康判级跳过 ``stale=1`` 的点，``over_count`` 不含 stale。

缺陷背景：参数一改，历史值就作废了（``stale=1``、趋势页画成灰色「待重算」），但健康树
仍拿作废值判级——最新点是个 stale 的越限值时节点照样报 alert，``over_count`` 还把 stale
点算进去。两处口径打架，用户被引去查设备。

修后口径：

- 判级只用 ``stale=0`` 的点；
- 整条序列都是 stale（或没有点）→ ``unset`` + ``needs_recompute``（「待重算」徽标），
  既不判正常也不判告警；
- ``over_count`` 明确不含 stale；``stale_count`` 与趋势页「待重算 N 条」同一个
  ``count_stale`` 口径（共用 ``_stale_where``）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lion_monitor.features import overview
from lion_monitor.features.health import node_health
from lion_monitor.store import datasets, limits, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import MetricValue

STAMP = "2026-11-04 10:00:00"
LIMIT = (2.8, 4.5, ">")  # warn 2.8 / alert 4.5（上限方向）


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def built(session, tmp_path):
    """任务 → 设备 → 对象 → CH1(v_RMS, a_Peak)：指标节点挂在 CH1 下。"""
    task = tasks.create_task(session, name="判级跳过stale")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    vrms = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=channel.id, attrs={"code": "v_RMS"}
    )
    apk = tree.create_node(
        session, task.id, "metric", "a_Peak", parent_id=channel.id, attrs={"code": "a_Peak"}
    )
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-04 09:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)
    return {
        "task": task,
        "root": root.id,
        "device": device.id,
        "component": comp.id,
        "channel": channel.id,
        "v_RMS": vrms.id,
        "a_Peak": apk.id,
        "file": file_row.id,
    }


def _add_points(session, built, values, *, stale=None, code="v_RMS"):
    """直接落点（一分钟一个点）；``stale`` 是与 values 等长的 0/1 列表。"""
    flags = list(stale) if stale is not None else [0] * len(values)
    assert len(flags) == len(values)
    base = datetime.strptime(STAMP, "%Y-%m-%d %H:%M:%S")
    for index, (value, flag) in enumerate(zip(values, flags, strict=True)):
        session.add(
            MetricValue(
                channel_id=built["file"],
                node_id=built["channel"],
                code=code,
                value=value,
                unit="mm/s",
                level="unset",
                computed_at=base + timedelta(minutes=index),
                stale=flag,
            )
        )
    session.commit()


def _limit(session, built):
    limits.set_limit(
        session, built["task"].id, built["channel"], "v_RMS", LIMIT[0], LIMIT[1], LIMIT[2]
    )


def test_all_stale_is_unset_with_recompute_flag(session, built):
    """整条序列都是待重算的越限值 → unset + 待重算，绝不判 alert。"""
    _limit(session, built)
    _add_points(session, built, [9.9, 9.9], stale=[1, 1])

    item = node_health(session, built["task"].id)[built["v_RMS"]]

    assert item.state == "unset"
    assert item.needs_recompute is True
    assert item.stale_count == 2
    assert item.over_count == 0
    assert item.latest_value is None, "没有有效点，不该拿 stale 值当最新值"


def test_only_alert_point_is_stale_state_is_not_alert(session, built):
    """唯一越限点是 stale、另有一个正常有效点 → 判 ok，不是 alert。"""
    _limit(session, built)
    _add_points(session, built, [1.0, 9.9], stale=[0, 1])

    item = node_health(session, built["task"].id)[built["v_RMS"]]

    assert item.state == "ok"
    assert item.over_count == 0
    assert item.stale_count == 1
    assert item.needs_recompute is False
    assert item.latest_value == pytest.approx(1.0), "最新值要取最近一个有效点"


def test_stale_points_are_not_counted_in_over_count(session, built):
    """3 个 stale 越限 + 2 个有效越限 → over_count = 2，state = alert。"""
    _limit(session, built)
    _add_points(session, built, [9.9, 9.9, 9.9, 9.9, 9.9], stale=[0, 0, 1, 1, 1])

    item = node_health(session, built["task"].id)[built["v_RMS"]]

    assert item.over_count == 2
    assert item.state == "alert"
    assert item.stale_count == 3


def test_health_stale_count_matches_trend_page_count(session, built):
    """「待重算 N 条」两处同一个口径：子树 self 汇总 == count_stale(task)。"""
    _limit(session, built)
    _add_points(session, built, [9.9, 9.9, 1.0], stale=[1, 1, 0])

    health = node_health(session, built["task"].id)

    assert health[built["v_RMS"]].stale_count == metric_store.count_stale(
        session, built["task"].id, node_id=built["channel"], code="v_RMS"
    )
    assert health[built["device"]].stale_count == 2
    # 这一条还有可判级的新点（state=ok），所以不打「待重算」徽标——徽标只在判不了级时出现
    assert health[built["device"]].needs_recompute is False


def test_parent_state_aggregates_children_and_keeps_unset_when_all_stale(session, built):
    """上层节点：子树里只有「全部待重算」的指标 → 状态仍是 unset + 待重算。"""
    _limit(session, built)
    _add_points(session, built, [9.9], stale=[1])

    health = node_health(session, built["task"].id)

    assert health[built["channel"]].state == "unset"
    assert health[built["channel"]].needs_recompute is True
    assert health[built["root"]].needs_recompute is True


def test_over_records_exclude_stale_rows(session, built):
    """工作台「超限记录」与 over_count 同口径：stale 行不出现在记录里。"""
    _limit(session, built)
    _add_points(session, built, [9.9, 5.0, 9.9], stale=[1, 0, 0])

    records = overview.over_records(session, built["task"].id)

    assert [round(row.value, 1) for row in records] == [5.0, 9.9]
    assert all(row.level == "alert" for row in records)


def test_node_without_values_stays_plain_unset(session, built):
    """一个点都没有 → unset，但没有「待重算」标记（没东西可重算）。"""
    item = node_health(session, built["task"].id)[built["a_Peak"]]

    assert item.state == "unset"
    assert item.needs_recompute is False
    assert item.stale_count == 0
