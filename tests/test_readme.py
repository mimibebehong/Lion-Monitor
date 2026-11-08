"""README 需求章节守卫：占位必须去掉，口径要与 PROJECT_CONTEXT 和已实现能力一致。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(title: str) -> str:
    """取某个二级标题下的正文（到下一个二级标题为止）。"""
    marker = f"\n## {title}\n"
    assert marker in README, f"README 缺少「{title}」章节"
    body = README.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def test_readme_has_substantive_requirements_section():
    body = _section("需求")
    assert len(body.strip()) > 800, "需求章节内容过短，疑似仍是占位"


def test_readme_placeholders_are_gone():
    for stale in ("待补充", "尚未填充", "P0 阶段", "占位页"):
        assert stale not in README, f"README 仍残留占位表述：{stale}"


def test_requirements_state_data_source_metrics_and_limits():
    body = _section("需求")
    for token in ("envision", ".dat", "v_RMS", "warn", "alert", "SQLite"):
        assert token in body, f"需求章节缺少口径：{token}"


def test_readme_points_to_the_open_items_doc():
    assert "2026-10-30-剩余工作盘点" in README
