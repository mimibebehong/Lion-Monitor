"""换页要回到「这一页的默认样子」（2026-11-05）。

Streamlit 会把控件状态一直留在 ``st.session_state`` 里，从健康树点走再点回来，
还停在上次选中的节点 / 展开状态 / 页签上，看着像「没刷新」。

规则在 ``app/nav_state.py``（纯 dict 逻辑，不碰 Streamlit，方便单测）：

- 换页 → 清掉页面级状态，只保留当前任务相关键和 ``__nav_page``；
- 同页重跑（点按钮、切页签）→ 什么都不清。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import nav_state  # noqa: E402


def test_same_page_keeps_state() -> None:
    state = {"__nav_page": "工作台", "ht_selected": "42", "task_selector": "TT-1"}
    assert nav_state.reset_if_page_changed(state, "工作台") is False
    assert state["ht_selected"] == "42"


def test_nav_clears_page_local_state() -> None:
    state = {
        "__nav_page": "回收站",
        "ht_selected": "42",
        "ht_selected__expanded": {1, 2},
        "ht_tabs_9": "子节点",
        "trend_window": "最近 90 天",
        "current_task_id": 7,
        "task_selector": "TT-1 · 示例",
        "current_task": "TT-1 · 示例",
    }

    assert nav_state.reset_if_page_changed(state, "健康树配置") is True

    assert "ht_selected" not in state
    assert "ht_selected__expanded" not in state
    assert "ht_tabs_9" not in state
    assert "trend_window" not in state
    assert state["__nav_page"] == "健康树配置"
    # 跨页面该记住的：当前任务
    assert state["current_task_id"] == 7
    assert state["task_selector"] == "TT-1 · 示例"
    assert state["current_task"] == "TT-1 · 示例"


def test_explicit_keep_overrides_default() -> None:
    state = {"__nav_page": "工作台", "scan_batches": [1], "other": 2}
    assert nav_state.reset_if_page_changed(state, "数据接入", keep={"scan_batches"}) is True
    assert state["scan_batches"] == [1]
    assert "other" not in state
