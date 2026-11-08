"""数据集管理页：保存修改与删除数据集同一排、单入口两次确认（马哥 2026-11-03）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "datasets.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "ds_page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="数据集任务")
        row = datasets.create_dataset(
            session,
            task.id,
            name="2026-10-29 15_28_34",
            source_dir="/tmp/envision/data/四通道保存/2026-10-29 15_28_34",
            collected_at="2026-10-29 15:28:34",
        )
        dataset_id = row.id

    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "task": task.id, "dataset_id": dataset_id}
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["dataset_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def test_save_and_delete_live_in_one_row(env):
    at = _app(env)
    labels = [button.label for button in at.button]
    assert "保存修改" in labels, labels
    assert "删除数据集" in labels, labels
    # 不再有勾选框那套「第二个删除入口」
    assert not any("确认删除该数据集" in str(box.label) for box in at.checkbox), [
        box.label for box in at.checkbox
    ]


def test_delete_needs_two_clicks(env):
    at = _app(env)
    next(button for button in at.button if button.label == "删除数据集").click().run()

    # 第一次点击只进入确认态，数据还在
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert any("确认删除" in button.label for button in at.button), [
        button.label for button in at.button
    ]
    with store_db.session_scope(env["factory"]) as session:
        assert datasets.get_dataset(session, env["dataset_id"]).id == env["dataset_id"]

    next(button for button in at.button if "确认删除" in button.label).click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    with store_db.session_scope(env["factory"]) as session:
        assert datasets.list_datasets(session, env["task"]) == []


def test_go_to_ingest_actually_navigates(env):
    """「＋ 去数据接入」要真的跳转到数据接入页，不能只是清除选中就完事。"""
    source = APP_PAGE.read_text(encoding="utf-8")
    assert 'st.switch_page("pages/ingest.py")' in source, "按钮没有真的跳转"
    assert 'st.Page("pages/ingest.py"' in (ROOT / "app" / "main.py").read_text(encoding="utf-8"), (
        "跳转目标不在导航里"
    )
