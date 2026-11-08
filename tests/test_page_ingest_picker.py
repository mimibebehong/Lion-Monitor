"""数据接入页：紧凑版式（2026-11-03）。

目录选择改为网页端浏览器的用例见 ``test_page_ingest_browse.py``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for extra in (ROOT / "src", APP):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import store_ctx  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("LION_DB", str(path))
    store_ctx._session_factory.clear()
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        tasks.create_task(session, name="接入任务")
    yield tmp_path
    store_ctx._session_factory.clear()


def _page() -> AppTest:
    at = AppTest.from_file(str(APP / "pages" / "ingest.py"), default_timeout=60)
    at.run()
    return at


def test_layout_is_compact(env):
    """KPI 卡已折进页头、格式说明进 tooltip：主功能区不再被顶出首屏。"""
    source = (APP / "pages" / "ingest.py").read_text(encoding="utf-8")

    assert "kpi_row(" not in source, "KPI 卡应折进页头 badge，否则主功能区被挤出首屏"
    assert "格式：envis .dat（512 B 头 + float32 样本）；一个子目录" not in source
    assert "📁 浏览目录" in source


def test_batch_actions_and_compute_checkbox_share_one_row(env):
    """全选三按钮 + 批次多选 + 「导入时计算指标」收成一行。"""
    source = (APP / "pages" / "ingest.py").read_text(encoding="utf-8")

    assert "b5.checkbox(" in source
    assert "b4.multiselect(" in source
