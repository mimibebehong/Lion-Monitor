"""``store.metrics.list_values`` 的「无归属历史行」开关（P2 / S1 的兼容口径）。

口径：

- ``list_values(..., node_id=X)`` 默认只给 X 自己的行（历史 ``node_id IS NULL`` 不混进来）
- ``list_values(..., node_id=X, include_unattributed=True)``：X 的行 + 无归属历史行
  —— 只有「该通道名在本任务下只有一个节点」时上层才会这么调，用来兼容老数据、不丢点
"""

from __future__ import annotations

import pytest

from lion_monitor.store import datasets, tasks, tree
from lion_monitor.store import db as store_db
from lion_monitor.store import metrics as metric_store
from lion_monitor.store.metrics import NodeValue

STAMP = "2026-11-01 10:00:00"
LEGACY_STAMP = "2026-10-30 10:00:00"


@pytest.fixture()
def session(tmp_path):
    engine = store_db.make_engine(tmp_path / "t.db")
    store_db.init_db(engine)
    with store_db.make_session_factory(engine)() as s:
        yield s


@pytest.fixture()
def channel(session, tmp_path):
    """任务 + 数据集 + 一个通道文件；返回通道文件行 id。"""
    task = tasks.create_task(session, name="无归属行")
    tree.root_node(session, task.id)
    folder = tmp_path / "ds"
    folder.mkdir()
    ds = datasets.create_dataset(session, task.id, "批次", folder, "2026-11-01 09:00:00")
    file_row = datasets.add_file(session, ds.id, folder / "CH1_Task1.dat", "CH1", 32000.0, 64000)
    return task, file_row


def _seed(session, file_id: int) -> None:
    metric_store.upsert_values(session, file_id, {"v_RMS": 1.5}, {"v_RMS": "mm/s"}, LEGACY_STAMP)
    metric_store.upsert_node_values(
        session,
        file_id,
        STAMP,
        [
            NodeValue(node_id=11, code="v_RMS", value=3.2),
            NodeValue(node_id=12, code="v_RMS", value=1.8),
        ],
    )


def test_include_unattributed_returns_own_rows_plus_legacy(session, channel):
    task, file_row = channel
    _seed(session, file_row.id)

    rows = metric_store.list_values(
        session, task.id, "CH1", "v_RMS", node_id=11, include_unattributed=True
    )

    assert [row.value for row in rows] == [pytest.approx(1.5), pytest.approx(3.2)]
    assert [row.node_id for row in rows] == [None, 11]


def test_include_unattributed_does_not_leak_other_nodes(session, channel):
    task, file_row = channel
    _seed(session, file_row.id)

    rows = metric_store.list_values(
        session, task.id, "CH1", "v_RMS", node_id=12, include_unattributed=True
    )

    assert [row.value for row in rows] == [pytest.approx(1.5), pytest.approx(1.8)]
    assert 11 not in {row.node_id for row in rows}


def test_default_still_strict(session, channel):
    task, file_row = channel
    _seed(session, file_row.id)

    assert [
        row.value for row in metric_store.list_values(session, task.id, "CH1", "v_RMS", node_id=11)
    ] == [pytest.approx(3.2)]
