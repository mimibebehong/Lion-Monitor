"""表格选中行号越界（删掉一行后旧选中态还在）不能崩：selectable_table 要过滤越界行号。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_def, tasks  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "stale.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="越界任务")
        metric_def.seed_defaults(session, task.id)

    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "task": task.id}
    store_ctx._session_factory.clear()


def test_selectable_table_drops_out_of_range_selection(monkeypatch):
    from app import ui

    class _Sel:
        rows = [1, 3]

    class _Event:
        selection = _Sel()

    monkeypatch.setattr(ui.st, "dataframe", lambda *a, **k: _Event())

    df = pd.DataFrame({"指标码": ["v_RMS", "a_Peak", "a_RMS"]})
    assert ui.selectable_table(df, key="probe") == [1]


def test_metrics_page_survives_stale_row_selection(env):
    """删掉指标后旧选中态指向越界行，页面应回落到「新增指标」而不是 IndexError。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "metrics.py"), default_timeout=180).run()
    at.session_state["metric_table"] = {"selection": {"rows": [99], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    text = " ".join(str(item.value) for item in at.markdown)
    assert "新增指标" in text, text[:200]


def test_delete_selected_metric_does_not_crash(env):
    """马哥实测场景：选中第 4 行（自定义指标）→ 删除 → 表格变 3 行，页面不能 IndexError。"""
    from streamlit.testing.v1 import AppTest

    factory = env["factory"]
    with store_db.session_scope(factory) as session:
        metric_def.create_metric(session, env["task"], "kurtosis", unit="")

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "metrics.py"), default_timeout=180).run()
    at.session_state["metric_table"] = {"selection": {"rows": [3], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    # 马哥 2026-11-04：去掉勾选框，移除改二次点击
    next(button for button in at.button if button.label == "从台账移除").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    next(button for button in at.button if "确认移除" in button.label).click().run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    with store_db.session_scope(factory) as session:
        codes = [row.code for row in metric_def.list_metrics(session, env["task"])]
    assert codes == ["v_RMS", "a_Peak", "a_RMS"], codes
