"""T2 页面入口：「立即备份数据库」按钮点击后真产出文件，并显示最近备份。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
BACKUP_PAGE = ROOT / "app" / "pages" / "backup.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "lion.db"
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    engine.dispose()
    monkeypatch.setenv("LION_DB", str(path))
    monkeypatch.delenv("LION_BACKUP_DIR", raising=False)
    return path


def _texts(at: AppTest) -> str:
    """页面所有可见文案（正文 + caption + 各类提示框）。"""
    parts: list[str] = []
    for group in (at.markdown, at.caption, at.success, at.info, at.warning, at.error):
        parts.extend(str(item.value) for item in group)
    return " ".join(parts)


def test_backup_page_button_creates_file(db_file):
    at = AppTest.from_file(str(BACKUP_PAGE))
    at.run()
    assert not at.exception
    labels = [button.label for button in at.button]
    assert any("立即备份数据库" in label for label in labels), labels
    assert not (db_file.parent / "backups").exists()

    at.button(key="backup_now").click().run()

    assert not at.exception
    files = sorted((db_file.parent / "backups").glob("lion-*.db"))
    assert len(files) == 1
    # 页面把刚生成的文件（名字 / 大小）显示出来，而不是只说一句「已备份」
    page_text = _texts(at)
    assert files[0].name in page_text
    assert "最近备份" in page_text
