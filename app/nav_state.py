"""换页时清掉上一个页面的控件状态（马哥 2026-11-05）。

Streamlit 会把控件状态一直留在 ``st.session_state`` 里，所以从健康树点走再点回来，
还停在上次选中的节点 / 展开状态 / 页签上，看着像「没刷新」。这里在页面脚本执行前
把上一个页面的页面级状态清掉，只保留跨页面该记住的键。

逻辑单独放一个模块（不碰 Streamlit），纯 dict 就能测。
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping

#: 当前页面在 session_state 里的识别键
NAV_KEY = "__nav_page"

#: 换页也要保住的键：当前测试任务 + 本页识别键
KEEP_ON_NAV = frozenset({"current_task", "current_task_id", "task_selector", NAV_KEY})


def reset_if_page_changed(
    state: MutableMapping[str, object],
    page_key: str,
    keep: Iterable[str] = KEEP_ON_NAV,
) -> bool:
    """换页了就清掉页面级状态，返回是否真的清过。

    ``st.session_state`` 和普通 dict 都支持（只需要 get / 迭代 / 删除）。
    """
    if state.get(NAV_KEY) == page_key:
        return False
    state[NAV_KEY] = page_key
    protected = set(keep)
    for key in [item for item in state if item not in protected]:
        del state[key]
    return True
