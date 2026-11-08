"""首次建库播种：空库时建示例任务 + 卷绕产线健康树。

只在完全空库时播种；用户把任务全删了也不会自动复活（避免"删掉又冒出来"）。
示例树同时作为"恢复内置示例树"的模板来源。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import TestTask
from .tasks import TaskRow, create_task, update_task
from .tree import import_template, list_nodes

__all__ = [
    "EXAMPLE_TASK_NAME",
    "bootstrap_example_task",
    "bootstrap_example_tree_payload",
    "ensure_example_tree",
]

#: 示例任务名（与 UI 示例数据一致）
EXAMPLE_TASK_NAME = "卷绕产线振动监测"

#: 每通道三个指标（与内置示例树一致：v_RMS / a_Peak / a_RMS）
_METRICS: tuple[tuple[str, str], ...] = (
    ("v_RMS", "mm/s"),
    ("a_Peak", "g"),
    ("a_RMS", "g"),
)


def _metric_children() -> list[dict[str, Any]]:
    return [
        {"type": "metric", "name": code, "attrs": {"code": code, "单位": unit}, "children": []}
        for code, unit in _METRICS
    ]


def bootstrap_example_tree_payload() -> dict[str, Any]:
    """内置示例树（卷绕产线：1 设备 3 监测对象 4 通道 12 指标）。"""
    return {
        "schema": "lion-monitor/health-tree",
        "version": 1,
        "root": {
            "type": "task",
            "name": EXAMPLE_TASK_NAME,
            "attrs": {},
            "children": [
                {
                    "type": "device",
                    "name": "卷绕产线",
                    "attrs": {"工位": "WL-1", "产线": "卷绕"},
                    "children": [
                        {
                            "type": "component",
                            "name": "隔膜过辊轴承",
                            "attrs": {"类型": "轴承", "部位": "隔膜过辊", "传感器数": 2},
                            "children": [
                                {
                                    "type": "channel",
                                    "name": "CH1",
                                    "attrs": {},
                                    "children": _metric_children(),
                                },
                                {
                                    "type": "channel",
                                    "name": "CH2",
                                    "attrs": {},
                                    "children": _metric_children(),
                                },
                            ],
                        },
                        {
                            "type": "component",
                            "name": "入片牵引电机",
                            "attrs": {"类型": "电机", "部位": "入片牵引", "传感器数": 1},
                            "children": [
                                {
                                    "type": "channel",
                                    "name": "CH3",
                                    "attrs": {},
                                    "children": _metric_children(),
                                },
                            ],
                        },
                        {
                            "type": "component",
                            "name": "卷绕张力气缸",
                            "attrs": {"类型": "气缸", "部位": "卷绕张力", "传感器数": 1},
                            "children": [
                                {
                                    "type": "channel",
                                    "name": "CH4",
                                    "attrs": {},
                                    "children": _metric_children(),
                                },
                            ],
                        },
                    ],
                }
            ],
        },
    }


def bootstrap_example_task(session: Session) -> TaskRow | None:
    """空库时建示例任务 + 示例树并置为 ready；已有任何任务（含已软删除）则返回 None。"""
    existing = session.execute(select(func.count()).select_from(TestTask)).scalar_one()
    if existing:
        return None

    row = create_task(session, name=EXAMPLE_TASK_NAME, operator="mark")
    import_template(session, row.id, bootstrap_example_tree_payload())
    return update_task(session, row.id, status="ready")


def ensure_example_tree(session: Session, task_id: int) -> dict[str, int]:
    """任务还没有任何节点时补一棵示例树（界面上"恢复内置示例树"也用它）。"""
    if list_nodes(session, task_id):
        return {}
    return import_template(session, task_id, bootstrap_example_tree_payload())
