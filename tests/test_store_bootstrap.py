"""任务列表子模块：首次建库时的示例任务播种（bootstrap）。

播种规则：库里一个任务都没有（含已软删除）时才建示例任务，避免用户清空后自动复活。
"""

from __future__ import annotations

import pytest

from lion_monitor.store import tasks
from lion_monitor.store.bootstrap import bootstrap_example_task, bootstrap_example_tree_payload
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "lion.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def test_empty_db_gets_example_task(session):
    row = bootstrap_example_task(session)

    assert row is not None
    assert row.name == "卷绕产线振动监测"
    assert row.status == "ready"
    assert [task.code for task in tasks.list_tasks(session)] == [row.code]


def test_example_task_codes_match_demo_template(session):
    row = bootstrap_example_task(session)

    assert row.code.startswith("TT-")
    assert row.code.endswith("-01")
    assert row.operator == "mark"


def test_bootstrap_is_idempotent(session):
    first = bootstrap_example_task(session)

    second = bootstrap_example_task(session)

    assert first is not None
    assert second is None
    assert len(tasks.list_tasks(session)) == 1


def test_bootstrap_skips_when_task_exists(session):
    tasks.create_task(session, name="已有任务")

    assert bootstrap_example_task(session) is None
    assert [task.name for task in tasks.list_tasks(session)] == ["已有任务"]


def test_bootstrap_does_not_revive_after_all_deleted(session):
    row = bootstrap_example_task(session)
    tasks.soft_delete_task(session, row.id)

    assert bootstrap_example_task(session) is None
    assert tasks.list_tasks(session) == []


def test_bootstrap_seeds_example_tree(session):
    from lion_monitor.store import tree

    row = bootstrap_example_task(session)
    snap = tree.tree_snapshot(session, row.id)

    assert snap["name"] == "卷绕产线振动监测"
    device = snap["children"][0]
    assert device["name"] == "卷绕产线"
    assert [comp["name"] for comp in device["children"]] == [
        "隔膜过辊轴承",
        "入片牵引电机",
        "卷绕张力气缸",
    ]
    channels = [ch["name"] for comp in device["children"] for ch in comp["children"]]
    assert channels == ["CH1", "CH2", "CH3", "CH4"]
    metrics = [
        metric["name"]
        for comp in device["children"]
        for ch in comp["children"]
        for metric in ch["children"]
    ]
    assert metrics.count("v_RMS") == 4
    assert metrics.count("a_Peak") == 4
    assert metrics.count("a_RMS") == 4


def test_example_tree_payload_is_importable_template(session):
    from lion_monitor.store import tree

    payload = bootstrap_example_tree_payload()

    assert payload["schema"] == "lion-monitor/health-tree"
    assert payload["version"] == 1
    assert payload["root"]["name"] == "卷绕产线振动监测"

    task = tasks.create_task(session, name="空任务")
    report = tree.import_template(session, task.id, payload)
    assert report.node_counts["device"] == 1
    assert report.node_counts["channel"] == 4
    assert report.node_counts["metric"] == 12
