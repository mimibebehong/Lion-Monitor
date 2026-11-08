"""旧数据兼容：库里 direction="<" 的老行 —— 读路径能解析、判级不变、数值不改。

历史背景：旧版 ``limits.set_limit`` 硬校验 ``warn < alert``，所以老库里 direction="<" 的行
可能两种都出现（模板层写入的 warn>alert 合法行、以及旧硬校验残留的 warn<alert 反向行）。
口径：迁移/读取**不改数值**；反向行只标记出来让人工确认。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from lion_monitor import limits_rules
from lion_monitor.features.health import point_level
from lion_monitor.store import limits, tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory
from lion_monitor.store.models import MetricLimit


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "legacy.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def chain(session):
    task = tasks.create_task(session, name="旧库任务")
    root = tree.root_node(session, task.id)
    device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
    ch = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
    metric = tree.create_node(
        session, task.id, "metric", "油压", parent_id=ch.id, attrs={"code": "油压"}
    )
    return task, ch, metric


def _insert_raw(session, task_id: int, node_id: int, code: str, warn: float, alert: float, d: str):
    """绕过 set_limit 直接写一行，模拟历史库里的老数据。"""
    model = MetricLimit(
        task_id=task_id,
        node_id=node_id,
        metric_code=code,
        warn=warn,
        alert=alert,
        direction=d,
        enabled=1,
    )
    session.add(model)
    session.commit()
    return model


def test_legacy_lower_row_is_readable_and_grades_the_same(session, chain):
    task, _, metric = chain
    _insert_raw(session, task.id, metric.id, "油压", 3.0, 1.0, "<")

    limit = limits.resolve_limit(session, task.id, metric.id, "油压")

    assert limit is not None
    assert limit[0] == pytest.approx(3.0)
    assert limit[1] == pytest.approx(1.0)
    assert limits_rules.normalize_direction(limit[2]) == "lower"
    # 迁移前后判级完全一致（越小越差：3.2 正常 / 2.5 警告 / 0.8 报警）
    assert point_level(3.2, limit) == "ok"
    assert point_level(2.5, limit) == "warn"
    assert point_level(0.8, limit) == "alert"


def test_legacy_reversed_lower_row_is_flagged_and_values_untouched(session, chain):
    """旧硬校验残留：direction="<" 但 warn<alert。数值不动，判级按老语义（先判 alert）。"""
    task, _, metric = chain
    _insert_raw(session, task.id, metric.id, "油压", 0.2, 0.5, "<")

    limit = limits.resolve_limit(session, task.id, metric.id, "油压")

    assert (limit[0], limit[1]) == (pytest.approx(0.2), pytest.approx(0.5))
    assert limits_rules.problem_of(limit[2], limit[0], limit[1]) is not None
    # 老语义：先判 alert（值 <= alert），再判 warn —— 与改前 point_level 保持一致
    assert point_level(0.1, limit) == "alert"
    assert point_level(0.3, limit) == "alert"
    assert point_level(0.6, limit) == "ok"

    stored = session.execute(select(MetricLimit.warn, MetricLimit.alert)).one()
    assert (stored[0], stored[1]) == (pytest.approx(0.2), pytest.approx(0.5))
    assert session.execute(select(func.count()).select_from(MetricLimit)).scalar_one() == 1


def test_legacy_row_not_rewritten_by_listing(session, chain):
    task, _, metric = chain
    _insert_raw(session, task.id, metric.id, "油压", 0.2, 0.5, "<")

    rows = limits.list_limits(session, task.id)

    assert len(rows) == 1
    assert rows[0].direction == "<"
    assert (rows[0].warn, rows[0].alert) == (pytest.approx(0.2), pytest.approx(0.5))
