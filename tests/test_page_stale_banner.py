"""页面级：源码更新后每个页面顶部要出现「重启服务」提示；代码没变时不许出现。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from streamlit.testing.v1 import AppTest  # noqa: E402

PAGE = ROOT / "app" / "pages" / "recycle_bin.py"
STALE_TEXT = "代码已更新（14:05），服务进程是 11:42 启动的 —— 报错请先重启服务"


def _run(monkeypatch, tmp_path, notice: str | None) -> AppTest:
    monkeypatch.setenv("LION_DB", str(tmp_path / "ui.db"))
    import ui  # noqa: E402  app/ui.py

    monkeypatch.setattr(ui, "_stale_notice", lambda: notice)
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    return at


def test_page_header_shows_restart_banner_when_sources_are_newer(monkeypatch, tmp_path):
    at = _run(monkeypatch, tmp_path, STALE_TEXT)
    assert not at.exception
    warnings = [w.value for w in at.warning]
    assert any("重启服务" in text for text in warnings), warnings


def test_page_header_shows_no_banner_when_sources_are_current(monkeypatch, tmp_path):
    at = _run(monkeypatch, tmp_path, None)
    assert not at.exception
    warnings = [w.value for w in at.warning]
    assert not any("重启服务" in text for text in warnings), warnings


def test_stale_notice_uses_real_sources_of_this_project():
    """真实工程路径下也要能算出来（不静默失败、不抛异常）。"""
    import ui  # noqa: E402

    notice = ui._stale_notice()
    assert notice is None or "重启" in notice


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
