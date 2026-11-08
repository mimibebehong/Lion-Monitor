"""G10 回归：``NaN`` 被判成 ``normal``（页面显示绿色「正常」）。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G10；probes C3）：

- ``limits_rules.grade(NaN, '>', 1.5, 3.0)`` 实测返回 ``normal``
- ``point_level(NaN, …)`` → ``ok`` → 趋势点标绿、健康评级「正常」

口径（本批定）：``grade`` 先判有限性 → 非有限值返回 ``unset``（不参与比较）；
``point_level`` 有阈值但值非有限 → 返回专门状态 ``invalid``（页面文案「数据异常」），
所有消费方（LEVEL_ORDER / STATE_TEXT / 健康分布 / 趋势配色）都要认得这个状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor import limits_rules  # noqa: E402
from lion_monitor.features import overview  # noqa: E402
from lion_monitor.features.health import LEVEL_ORDER, node_health, point_level  # noqa: E402
from lion_monitor.store import datasets, limits, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.db import init_db, make_engine, make_session_factory  # noqa: E402

FS = 32000.0
BAD_VALUES = (float("nan"), float("inf"), float("-inf"))


@pytest.mark.parametrize("value", BAD_VALUES)
def test_grade_of_non_finite_is_unset(value):
    assert limits_rules.grade(value, ">", 1.5, 3.0) == "unset"
    assert limits_rules.grade(value, "<", 3.0, 1.5) == "unset"
    assert limits_rules.grade(value, "upper", 1.5, 3.0) == "unset"


def test_grade_of_finite_values_unchanged():
    """有限值的判级口径一个字都不许变（回归）。"""
    assert limits_rules.grade(1.0, ">", 1.5, 3.0) == "normal"
    assert limits_rules.grade(2.0, ">", 1.5, 3.0) == "warn"
    assert limits_rules.grade(9.0, ">", 1.5, 3.0) == "alert"
    assert limits_rules.grade(2.0, "<", 3.0, 1.0) == "warn"
    assert limits_rules.grade(0.5, "<", 3.0, 1.0) == "alert"


@pytest.mark.parametrize("value", BAD_VALUES)
def test_point_level_of_non_finite_is_invalid(value):
    assert point_level(value, (1.5, 3.0, ">", "指标级")) == "invalid"


def test_point_level_without_limit_stays_unset():
    assert point_level(float("nan"), None) == "unset"
    assert point_level(1.0, (1.5, 3.0, ">", "指标级")) == "ok"


def test_ui_knows_invalid_state():
    """页面文案与配色都要认得 invalid（不然 STATE_TEXT[...] 直接 KeyError / 显示绿色）。"""
    from tree_ui import STATE_COLOR, STATE_TEXT

    assert STATE_TEXT["invalid"] == "数据异常"
    assert STATE_COLOR["invalid"] not in {
        STATE_COLOR["ok"],
        STATE_COLOR["warn"],
        STATE_COLOR["alert"],
    }
    assert set(STATE_TEXT) == set(LEVEL_ORDER)


def test_overview_counts_include_invalid():
    assert "invalid" in overview.HEALTH_STATES


def test_level_order_ranks_invalid_between_ok_and_warn():
    """汇总时「数据异常」不能被「正常」盖掉，但也不能盖过警告/严重。"""
    assert LEVEL_ORDER["unset"] < LEVEL_ORDER["ok"]
    assert LEVEL_ORDER["ok"] < LEVEL_ORDER["invalid"]
    assert LEVEL_ORDER["invalid"] < LEVEL_ORDER["warn"]
    assert LEVEL_ORDER["warn"] < LEVEL_ORDER["alert"]


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "nan.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def test_node_health_marks_infinite_value_as_invalid(session, tmp_path):
    """库里出现非有限值（例如历史脏数据 / 手工改库）：节点状态为 invalid，不是 ok。"""
    task = tasks.create_task(session, name="任务NaN")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric = tree.create_node(
        session, task.id, "metric", "v_RMS", parent_id=ch.id, attrs={"code": "v_RMS"}
    )
    limits.set_limit(session, task.id, metric.id, "v_RMS", 2.8, 4.5, ">")

    folder = tmp_path / "批" / "20261029152834"
    stamp = "20261029152834"
    write_dat(
        folder / f"{stamp}-CH1_Task1.dat",
        samples=np.zeros(int(FS), dtype="float32"),
        channel="CH1",
        started_at=stamp,
        sample_rate_hz=FS,
    )
    row = datasets.import_from_dir(session, task.id, folder)
    file = datasets.list_files(session, row.id)[0]
    metric_store.upsert_values(session, file.id, {"v_RMS": float("inf")}, {}, row.collected_at)

    health = node_health(session, task.id)

    assert health[metric.id].state == "invalid"
    assert health[ch.id].state == "invalid"
    assert health[comp.id].state == "invalid"
