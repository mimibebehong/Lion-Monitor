"""页头标签不能互相压住（2026-11-04 截图：任务号与任务状态重叠）。

根因：Streamlit 给「含原始 HTML 的 markdown 容器」加 `margin-bottom:-16px`，
拆成多个 `st.markdown` 时父层只分配 24px，第二行标签就压到第一行上。
正解：chip / badge / demo 三个标签放**同一个 markdown 块**，并抵消负边距。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app" / "ui.py"


def _page_header_body() -> str:
    src = UI.read_text(encoding="utf-8")
    return src[
        src.index("def page_header(") : src.index(
            "# ----------------", src.index("def page_header(")
        )
    ]


def test_page_header_renders_one_markdown_block_for_chips():
    """右侧标签区（with right: 到注入样式之前）只能有一次 markdown。"""
    body = _page_header_body()
    right_block = body[body.index("with right:") : body.index("st.markdown(PAGE_HEADER_CSS")]

    assert right_block.count("st.markdown(") == 1, "chip / badge / demo 必须合成一次 markdown"
    assert "ph-right" in right_block


def test_page_header_css_cancels_negative_margin():
    src = UI.read_text(encoding="utf-8")

    assert '[data-testid="stMarkdownContainer"]:has(.ph-right){margin-bottom:0 !important}' in src
    assert '[data-testid="stElementContainer"]:has(.ph-right){height:auto !important}' in src
