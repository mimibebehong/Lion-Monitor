"""重算要能把「逐窗前的老整段单值」一并作废。

那批值是 node_id 为空的遗留行（整段算一个值），不属于任何节点，重算原先按
``node_id == 本节点`` 找旧点，永远碰不到它们 → 趋势里会多出卡在记录起点的旧点
（30 个新点 + 3 个旧点 = 33）。马哥 2026-11-02 确认：可以按这个办法清掉。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics, tasks  # noqa: E402
from lion_monitor.store.metrics import NodeValue  # noqa: E402
from lion_monitor.store.models import Channel, Dataset, MetricValue, TaskNode  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    path = tmp_path / "legacy.db"
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="遗留值清理")
        ds = Dataset(task_id=task.id, code="DS-1", name="rec", channel_count=1)
        session.add(ds)
        session.flush()
        ch = Channel(
            dataset_id=ds.id,
            code="CH2",
            source_path="/tmp/x.dat",
            sample_rate_hz=32000.0,
            sample_count=640000,
            duration_s=20.0,
        )
        session.add(ch)
        session.flush()
        node = TaskNode(
            task_id=task.id, node_type="channel", name="CH2", path=f"{task.id}/9001", sort=0
        )
        session.add(node)
        session.flush()
        start = datetime(2026, 10, 29, 15, 28, 34)
        legacy = MetricValue(
            channel_id=ch.id,
            node_id=None,  # 老流程：整段单值，没有节点
            code="v_RMS",
            value=6.23,
            unit="mm/s",
            level="unset",
            computed_at=start,
            params_hash="",
            stale=0,
        )
        other_code = MetricValue(
            channel_id=ch.id,
            node_id=None,
            code="a_Peak",
            value=0.83,
            unit="g",
            level="unset",
            computed_at=start,
            params_hash="",
            stale=0,
        )
        session.add_all([legacy, other_code])
        session.flush()
        ids = {
            "channel": ch.id,
            "node": node.id,
            "legacy": legacy.id,
            "other_code": other_code.id,
            "start": start,
        }
    yield {"factory": factory, **ids}


def _points(start: datetime, count: int) -> list[tuple[str, NodeValue]]:
    return [
        (
            (start + timedelta(seconds=2 * index + 1)).strftime("%Y-%m-%d %H:%M:%S"),
            NodeValue(node_id=1, code="v_RMS", value=float(index), unit="mm/s"),
        )
        for index in range(count)
    ]


def test_recompute_retires_legacy_whole_record_value(env):
    with store_db.session_scope(env["factory"]) as session:
        written, retired = metrics.replace_node_series(
            session, env["channel"], env["node"], "v_RMS", _points(env["start"], 10)
        )
    assert written == 10
    assert retired == 1, "老整段单值要被作废"

    with store_db.session_scope(env["factory"]) as session:
        row = session.get(MetricValue, env["legacy"])
        assert row.deleted_at is not None
        # 别的指标的老值不动
        assert session.get(MetricValue, env["other_code"]).deleted_at is None
        alive_v_rms = (
            session.query(MetricValue)
            .filter(
                MetricValue.code == "v_RMS",
                MetricValue.deleted_at.is_(None),
            )
            .count()
        )
    assert alive_v_rms == 10, "v_RMS 只留本次重算出来的 10 个点"


def test_second_recompute_does_not_double_retire(env):
    with store_db.session_scope(env["factory"]) as session:
        metrics.replace_node_series(
            session, env["channel"], env["node"], "v_RMS", _points(env["start"], 10)
        )
    with store_db.session_scope(env["factory"]) as session:
        written, retired = metrics.replace_node_series(
            session, env["channel"], env["node"], "v_RMS", _points(env["start"], 10)
        )
    assert written == 10
    assert retired == 0
