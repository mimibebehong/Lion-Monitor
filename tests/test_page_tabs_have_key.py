"""所有 st.tabs 必须带 key（马哥 2026-11-03：点按钮后要回到原 tab）。

Streamlit 的 tabs 选中态是纯前端状态：**没有 key 时每次重跑都回到第一个页签**，
带 key 后由客户端按 key 保留。所以这里做源码级守卫，新增 tab 页时要一起加 key。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[1] / "app" / "pages"


def _calls() -> list[tuple[str, str]]:
    """返回 [(文件, 整个 st.tabs(...) 调用文本)]。"""
    found: list[tuple[str, str]] = []
    for path in sorted(PAGES.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"st\.tabs\(", text):
            depth, index = 0, match.end() - 1
            while index < len(text):
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            found.append((path.name, text[match.start() : index + 1]))
    return found


@pytest.mark.parametrize(("page", "call"), _calls())
def test_tab_call_has_key(page: str, call: str) -> None:
    assert "key=" in call, f"{page} 的 st.tabs 没带 key，重跑后会跳回第一个页签：{call[:80]}"


def test_all_tab_pages_covered() -> None:
    """确认守卫真的扫到了页面里的 tab（防止正则失效后变成空跑）。"""
    calls = _calls()
    assert len(calls) >= 5, calls
    assert {page for page, _ in calls} >= {
        "trends.py",
        "tasks.py",
        "objects.py",
        "channel_analysis.py",
    }
