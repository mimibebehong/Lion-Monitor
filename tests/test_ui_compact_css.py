"""紧凑版式样式：只给「要多塞内容」的页面用（2026-11-03：绘图区不滚动就能看见）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "src"))

import ui  # noqa: E402


def test_compact_css_tightens_top_padding_and_captions():
    css = ui.compact_css()

    assert css.startswith("<style>") and css.endswith("</style>")
    assert 'data-testid="stMainBlockContainer"' in css
    assert "padding-top" in css
    # 说明行（st.caption）默认一行要 38px，压实到 20px 上下
    assert 'data-testid="stCaptionContainer"' in css
    assert "line-height" in css


def test_compact_css_shrinks_block_gap_and_labels():
    css = ui.compact_css()

    assert 'data-testid="stVerticalBlock"' in css
    assert "gap" in css
    assert 'data-testid="stWidgetLabel"' in css


def test_compact_css_keeps_header_clear_of_toolbar():
    """主区上边距不能压到 60px 的顶栏底下（否则标题被工具条盖住）。"""
    css = ui.compact_css()

    assert "padding-top: 4" in css, "上边距应压到 4rem 左右，而不是贴住顶栏"


def test_compact_css_is_opt_in_not_global():
    """不能塞进 page_header —— 那会对 13 个页面一起生效。"""
    source = (ROOT / "app" / "ui.py").read_text(encoding="utf-8")
    header_body = source[source.index("def page_header(") : source.index("def stepper(")]
    assert "compact_css" not in header_body
