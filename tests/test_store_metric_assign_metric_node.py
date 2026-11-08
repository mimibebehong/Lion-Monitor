"""指标参数卡落点 = 指标节点（马哥 2026-11-02：配置下放到指标节点）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store.metric_assign import MetricAssignError  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "ma_metric_node.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.path = path
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="参数落点")
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(session, self.task.id, "device", "产线", parent_id=root.id)
            comp = tree.create_node(
                session, self.task.id, "component", "3#辊压机", parent_id=device.id
            )
            self.channel = tree.create_node(
                session, self.task.id, "channel", "CH1", parent_id=comp.id
            )
            self.metric_v = tree.create_node(
                session,
                self.task.id,
                "metric",
                "v_RMS",
                parent_id=self.channel.id,
                attrs={"code": "v_RMS"},
            )
            self.metric_a = tree.create_node(
                session,
                self.task.id,
                "metric",
                "a_Peak",
                parent_id=self.channel.id,
                attrs={"code": "a_Peak"},
            )
            # 只有通道、没有指标节点的通道
            self.bare_channel = tree.create_node(
                session, self.task.id, "channel", "CH9", parent_id=comp.id
            )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def test_assign_via_channel_lands_on_metric_node(env):
    with env.session() as session:
        row = metric_assign.assign_metric(session, env.task.id, env.channel.id, "v_RMS")
        assert row.node_id == env.metric_v.id


def test_assign_via_metric_node_directly(env):
    with env.session() as session:
        row = metric_assign.assign_metric(session, env.task.id, env.metric_a.id, "a_Peak")
        assert row.node_id == env.metric_a.id


def test_assign_rejects_channel_without_metric_node(env):
    with env.session() as session:
        with pytest.raises(MetricAssignError, match="指标节点"):
            metric_assign.assign_metric(session, env.task.id, env.bare_channel.id, "v_RMS")


def test_assign_rejects_code_mismatch_on_metric_node(env):
    with env.session() as session:
        with pytest.raises(MetricAssignError):
            metric_assign.assign_metric(session, env.task.id, env.metric_v.id, "a_Peak")


def test_bind_missing_targets_metric_nodes(env):
    with env.session() as session:
        created, skipped = metric_assign.bind_missing(session, env.task.id)
        assert created == 2 and skipped == []
        rows = {row.code: row for row in metric_assign.list_assignments(session, env.task.id)}
        assert rows["v_RMS"].node_id == env.metric_v.id
        assert rows["a_Peak"].node_id == env.metric_a.id


def test_missing_bindings_reports_metric_node_ids(env):
    with env.session() as session:
        before = metric_assign.missing_bindings(session, env.task.id)
        assert {item[0] for item in before} == {env.metric_v.id, env.metric_a.id}
        metric_assign.assign_metric(session, env.task.id, env.channel.id, "v_RMS")
        after = metric_assign.missing_bindings(session, env.task.id)
        assert [item[0] for item in after] == [env.metric_a.id]


def _legacy_row(engine, task_id: int, node_id: int, code: str) -> int:
    """按老口径（node_id=通道节点）直接写一条绑定行。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO metric_assign (task_id, node_id, metric_code, unit,"
                " params_json, params_locked, enabled) VALUES"
                " (:t, :n, :c, 'mm/s', '{}', 0, 1)"
            ),
            {"t": task_id, "n": node_id, "c": code},
        )
        return int(conn.execute(text("SELECT last_insert_rowid()")).scalar_one())


def test_migration_moves_channel_rows_to_metric_nodes(env):
    legacy_id = _legacy_row(env.engine, env.task.id, env.channel.id, "v_RMS")

    store_db.init_db(env.engine)

    with env.session() as session:
        rows = {row.id: row for row in metric_assign.list_assignments(session, env.task.id)}
    assert rows[legacy_id].node_id == env.metric_v.id


def test_migration_soft_deletes_rows_without_metric_node(env):
    orphan = _legacy_row(env.engine, env.task.id, env.bare_channel.id, "v_RMS")

    store_db.init_db(env.engine)

    with env.session() as session:
        assert orphan not in {
            row.id for row in metric_assign.list_assignments(session, env.task.id)
        }


def test_migration_is_idempotent(env):
    _legacy_row(env.engine, env.task.id, env.channel.id, "v_RMS")
    store_db.init_db(env.engine)
    store_db.init_db(env.engine)

    with env.session() as session:
        rows = metric_assign.list_assignments(session, env.task.id)
    assert len(rows) == 1
    assert rows[0].node_id == env.metric_v.id
