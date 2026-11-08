"""Q9：指标定义默认值不再绕过 band_low 校验（真跑「指标配置」页）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
METRICS_PAGE = ROOT / "app" / "pages" / "metrics.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_def, tasks  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "defs.db"
        monkeypatch.setenv("LION_DB", str(path))
        engine = store_db.make_engine(path)
        store_db.init_db(engine)
        self.factory = store_db.make_session_factory(engine)
        with store_db.session_scope(self.factory) as session:
            self.task = tasks.create_task(session, name="指标定义任务")
            metric_def.seed_defaults(session, self.task.id)

    def v_rms_defaults(self) -> dict:
        with store_db.session_scope(self.factory) as session:
            rows = metric_def.list_metrics(session, self.task.id)
            row = next(r for r in rows if r.code == "v_RMS")
            return dict(row.defaults)

    def open_editor(self):
        import store_ctx
        from streamlit.testing.v1 import AppTest

        store_ctx._session_factory.clear()
        at = AppTest.from_file(str(METRICS_PAGE), default_timeout=120).run()
        at.session_state["metric_table"] = {"selection": {"rows": [0], "columns": []}}
        at.run()
        return at


@pytest.fixture()
def env(tmp_path, monkeypatch):
    yield Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()


def _defaults_box(at):
    return next(box for box in at.text_area if box.label == "默认值（JSON）")


def _save_button(at):
    return next(btn for btn in at.button if btn.label == "保存修改")


def test_definition_rejects_band_low_below_hard_floor(env):
    at = env.open_editor()
    before = env.v_rms_defaults()
    _defaults_box(at).set_value(
        json.dumps({"band_low": 0.1, "band_high": 1000.0, "window_s": 2.0, "step_s": 2.0})
    )
    _save_button(at).click().run()

    errors = [item.value for item in at.error]
    assert any("带通下限" in text for text in errors), errors
    assert env.v_rms_defaults()["band_low"] == before["band_low"]


def test_definition_raises_band_low_for_short_window(env):
    at = env.open_editor()
    _defaults_box(at).set_value(
        json.dumps({"band_low": 2.0, "band_high": 1000.0, "window_s": 2.0, "step_s": 2.0})
    )
    _save_button(at).click().run()

    assert [exc.value for exc in at.exception] == []
    assert env.v_rms_defaults()["band_low"] == 5.0
