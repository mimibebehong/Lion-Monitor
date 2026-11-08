"""页面级：「监测对象」新建表单的对象类型下拉要能选到 齿轮 / 轴 / 行星齿轮，并真能存库。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from streamlit.testing.v1 import AppTest  # noqa: E402

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import object_registry as reg  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402

PAGE = ROOT / "app" / "pages" / "objects.py"
NEW_TYPES = ("齿轮", "轴", "行星齿轮")


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("LION_DB", str(path))
    import store_ctx  # noqa: E402  app/store_ctx.py

    # cache_resource 会把上一个用例的 engine 带过来，必须先清
    store_ctx._session_factory.clear()
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="UI 任务")
        task_id = task.id
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    yield at, factory, task_id
    store_ctx._session_factory.clear()


def test_type_selectbox_offers_gear_shaft_planet_ring(app_env):
    at, _factory, _task_id = app_env
    assert not at.exception
    boxes = [b for b in at.selectbox if b.label == "对象类型"]
    assert boxes, "新建表单里应有「对象类型」下拉"
    options = list(boxes[0].options)
    for kind in NEW_TYPES:
        assert kind in options, (kind, options)


def test_create_object_with_planet_ring_persists(app_env):
    at, factory, task_id = app_env
    # 表单控件要「填值 → 点提交 → 一次性 run」；中间插 run 会把表单重新渲染、值被重置
    at.text_input[0].set_value("一级行星齿轮")
    [b for b in at.selectbox if b.label == "对象类型"][0].set_value("行星齿轮")
    [b for b in at.button if b.label == "创建对象"][0].click()
    at.run()
    assert not at.exception
    assert any("已创建" in s.value for s in at.success), [s.value for s in at.success]

    with store_db.session_scope(factory) as session:
        rows = reg.list_objects(session, task_id)
    assert [(r.name, r.obj_type) for r in rows] == [("一级行星齿轮", "行星齿轮")]
