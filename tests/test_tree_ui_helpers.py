"""健康树渲染层纯函数：遍历与状态兜底不依赖任何演示数据。

``tree_ui`` 提供给健康树页与趋势页共用，只接受调用方传入的嵌套快照
（来自 ``store.tree.tree_snapshot``），不得回落到演示数据或内置示例树。
"""

from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import tree_ui  # noqa: E402

SNAPSHOT = {
    "id": 1,
    "type": "task",
    "name": "任务",
    "attrs": {},
    "children": [
        {
            "id": 2,
            "type": "device",
            "name": "设备",
            "attrs": {},
            "children": [
                {"id": 3, "type": "component", "name": "轴承", "attrs": {}, "children": []},
                {"id": 4, "type": "component", "name": "电机", "attrs": {}, "children": []},
            ],
        }
    ],
}


def test_walk_visits_nodes_depth_first_with_depth():
    got = [(node["name"], depth) for node, depth in tree_ui.walk(SNAPSHOT)]
    assert got == [("任务", 0), ("设备", 1), ("轴承", 2), ("电机", 2)]


def test_walk_tolerates_missing_children_key():
    got = [node["name"] for node, _depth in tree_ui.walk({"name": "叶子"})]
    assert got == ["叶子"]


def test_state_of_prefers_injected_state():
    assert tree_ui.state_of({"type": "metric", "state": "warn"}) == "warn"
    assert tree_ui.state_of({"type": "device", "state": "alert"}) == "alert"


def test_state_of_without_state_is_unset():
    assert tree_ui.state_of({"type": "metric", "name": "v_RMS"}) == "unset"
    assert tree_ui.state_of({"type": "device", "children": []}) == "unset"


def test_state_of_ignores_unknown_state_value():
    assert tree_ui.state_of({"type": "channel", "state": "boom"}) == "unset"
