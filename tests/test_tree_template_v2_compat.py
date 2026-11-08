"""健康树模板 v2（T4）：v1 旧模板仍可导入，未知版本明确报错。

- v1：引用仍是自增 ``ref_id``，能读但跨任务易错挂 → 导入后提示「旧版模板，建议重新导出 v2」
- 未知版本（3、缺失、字符串）：明确报错，不做猜测
"""

from __future__ import annotations

from typing import Any

import pytest

from lion_monitor.store import (
    bootstrap,
    channel_reg,
    metric_def,
    object_registry,
    tasks,
    tree,
    tree_template,
)
from lion_monitor.store.db import init_db, make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path):
    engine = make_engine(tmp_path / "tpl_v2_compat.db")
    init_db(engine)
    with make_session_factory(engine)() as s:
        yield s


def _v1_payload() -> dict[str, Any]:
    """v1 外壳：结构 + attrs + 自增 ref_id，没有 params / thresholds / task / exported_at。"""
    return {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": "旧模板任务",
            "attrs": {},
            "children": [
                {
                    "type": "device",
                    "name": "卷绕产线",
                    "attrs": {"工位": "WL-1"},
                    "children": [
                        {
                            "type": "component",
                            "name": "隔膜过辊轴承",
                            "attrs": {"部位": "隔膜过辊"},
                            "ref_type": "monitor_object",
                            "ref_id": 1,
                            "children": [
                                {
                                    "type": "channel",
                                    "name": "CH1",
                                    "attrs": {},
                                    "ref_type": "channel_reg",
                                    "ref_id": 1,
                                    "children": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


def _seed_ledger(session, task_id: int) -> None:
    obj = object_registry.create_object(session, task_id, "隔膜过辊轴承", "轴承", part="隔膜过辊")
    channel_reg.create_channel(session, task_id, obj.id, "CH1")
    metric_def.seed_defaults(session, task_id)


def test_v1_template_still_imports_with_legacy_notice(session):
    task = tasks.create_task(session, name="新任务")
    _seed_ledger(session, task.id)

    report = tree.import_template(session, task.id, _v1_payload())

    assert report.version == 1
    assert report.node_counts == {"task": 1, "device": 1, "component": 1, "channel": 1}
    assert report.metrics_bound == 0
    assert "旧版模板" in report.notice and "v2" in report.notice
    names = [row.name for row in tree.list_nodes(session, task.id)]
    assert names == ["旧模板任务", "卷绕产线", "隔膜过辊轴承", "CH1"]


def test_v1_import_keeps_id_based_references(session):
    task = tasks.create_task(session, name="新任务")
    _seed_ledger(session, task.id)

    tree.import_template(session, task.id, _v1_payload())

    nodes = {row.name: row for row in tree.list_nodes(session, task.id)}
    assert nodes["隔膜过辊轴承"].ref_type == "monitor_object"
    assert nodes["隔膜过辊轴承"].ref_id == 1
    assert nodes["CH1"].ref_id == 1


def test_bootstrap_example_payload_v1_is_previewable_and_importable(session):
    task = tasks.create_task(session, name="空任务")
    payload = bootstrap.bootstrap_example_tree_payload()

    preview = tree.preview_import(session, task.id, payload)
    assert preview.version == 1
    assert preview.legacy is True
    assert "旧版模板" in preview.notice
    assert preview.node_counts["metric"] == 12

    report = tree.import_template(session, task.id, payload)
    assert report.node_counts["channel"] == 4


@pytest.mark.parametrize("version", [3, 0, "2", None])
def test_unknown_template_version_is_rejected(session, version):
    task = tasks.create_task(session, name="新任务")
    payload = _v1_payload()
    payload["version"] = version

    with pytest.raises(tree.TreeError, match="版本不支持"):
        tree.validate_template(payload)
    with pytest.raises(tree.TreeError, match="版本不支持"):
        tree.preview_import(session, task.id, payload)
    with pytest.raises(tree.TreeError, match="版本不支持"):
        tree.import_template(session, task.id, payload)


def test_missing_schema_is_rejected(session):
    payload = _v1_payload()
    payload.pop("schema")

    with pytest.raises(tree.TreeError, match="模板标识"):
        tree.import_template(session, tasks.create_task(session, name="X").id, payload)


def test_v2_payload_reports_no_legacy_notice(session):
    task = tasks.create_task(session, name="新任务")
    _seed_ledger(session, task.id)
    root = tree.root_node(session, task.id)
    tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
    payload = tree.export_template(session, task.id)

    preview = tree.preview_import(session, task.id, payload)

    assert preview.legacy is False
    assert preview.notice == ""
    assert preview.version == 2


def test_legacy_note_constant_is_user_facing():
    assert "重新导出" in tree_template.LEGACY_NOTE
