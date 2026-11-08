"""页头徽标不再重复当前任务号（马哥 2026-11-04）。

「📋 任务 TT-xxx」是全局上下文 chip，badge 只该放**本页增量**（条数/状态），
两边都带任务号就是纯重复。
"""

from __future__ import annotations

from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "app" / "pages"


def test_no_badge_repeats_task_code():
    offenders: list[str] = []
    for f in sorted(PAGES.glob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "badge=" in line and ("task.code" in line or "TASK.code" in line):
                offenders.append(f"{f.name}:{i}")
    assert not offenders, f"badge 仍在重复任务号：{offenders}"
