"""任务列表：保存 / 设为当前任务 / 归档 / 删除 同一排，且删除只有一个入口。

2026-11-02：
- 「为什么有两个删除」→ 去掉「确认删除」勾选框，改成同一个按钮点两次确认；
- 「保存修改、设为当前任务、归档、删除任务放到一行」→ 四个等宽按钮同一排。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "tasks.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "tasks_actions.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        tasks.create_task(session, name="卷绕产线振动监测")
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "path": path}
    store_ctx._session_factory.clear()


def _app_with_selection(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["task_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _labels(at):
    return [button.label for button in at.button]


def _active_codes(env):
    engine = store_db.make_engine(env["path"])
    with store_db.session_scope(store_db.make_session_factory(engine)) as session:
        return [row.code for row in tasks.list_tasks(session)]


def test_four_actions_in_one_row(env):
    at = _app_with_selection(env)
    labels = _labels(at)
    start = labels.index("保存修改")
    assert labels[start : start + 4] == ["保存修改", "设为当前任务", "归档", "删除任务"]


def test_only_one_delete_entry(env):
    """页面上不再出现「确认删除」勾选框，删除只有一个按钮入口。"""
    at = _app_with_selection(env)
    assert not [box for box in at.checkbox if box.label == "确认删除"]
    assert _labels(at).count("删除任务") == 1


def test_delete_needs_second_click(env):
    at = _app_with_selection(env)
    at.button(key="delete_1").click().run()
    # 第一次点击只是进入待确认，任务仍在
    assert "确认删除？" in _labels(at)
    assert _active_codes(env) != []

    at.button(key="delete_1").click().run()
    assert _active_codes(env) == []


def test_switch_away_clears_pending_delete(env):
    at = _app_with_selection(env)
    at.button(key="delete_1").click().run()
    assert "确认删除？" in _labels(at)
    # 取消选中再选回来，红色确认态应被清掉
    at.session_state["task_table"] = {"selection": {"rows": [], "columns": []}}
    at.run()
    at.session_state["task_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert "删除任务" in _labels(at)


def test_section_is_named_edit_and_actions(env):
    at = _app_with_selection(env)
    headings = [str(item.value) for item in at.markdown if str(item.value).startswith("####")]
    assert any("编辑与操作" in text for text in headings), headings
    assert not any(text.strip() == "#### 操作" for text in headings), headings


def test_four_buttons_have_no_extra_color_by_default(env):
    """现场：正常态四个按钮都不加配色，只有悬停/按下才变蓝。"""
    at = _app_with_selection(env)
    for key in ("save_1", "make_current_1", "archive_1", "delete_1"):
        assert at.button(key=key).proto.type != "primary", f"{key} 正常态不应是主色"
    styles = " ".join(str(item.value) for item in at.markdown)
    assert "#4c9be8" in styles, "应有悬停变蓝的样式"
    assert ":hover" in styles and ":active" in styles
    assert "task-actions-1" in styles, "样式必须限定在四个按钮的容器内"


def test_confirm_delete_turns_red_not_blue(env):
    """待确认删除只变红，且不占用主色（蓝色只由悬停触发）。"""
    at = _app_with_selection(env)
    at.button(key="delete_1").click().run()
    delete = at.button(key="delete_1")
    assert delete.label == "确认删除？"
    assert delete.proto.type != "primary"
    styles = " ".join(str(item.value) for item in at.markdown)
    assert "#d93a3a" in styles, "待确认时应注入红色样式"
    assert ".st-key-task_del_btn_1 button" in styles, "红色样式必须只作用在删除按钮上"


def test_switch_current_task_uses_shared_flash(env):
    """操作提醒统一走 ui.flash（页面标题下方），不再用右下角 toast。"""
    at = _app_with_selection(env)
    at.button(key="make_current_1").click().run()
    messages = [str(item.value) for item in at.success]
    assert any("当前任务切换为" in text for text in messages), messages


def test_new_task_comes_with_builtin_metrics(env):
    """2026-11-03：新建任务默认补齐三个内置指标，页面不再需要「补齐内置指标」按钮。"""
    from streamlit.testing.v1 import AppTest

    from lion_monitor.store import metric_def

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    next(i for i in at.text_input if i.label.startswith("任务名称")).set_value("自动补指标任务")
    next(b for b in at.button if b.label == "创建任务").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    codes = []
    engine = store_db.make_engine(env["path"])
    with store_db.session_scope(store_db.make_session_factory(engine)) as session:
        for row in tasks.list_tasks(session):
            if row.name == "自动补指标任务":
                codes = [m.code for m in metric_def.list_metrics(session, row.id)]
    assert codes == ["v_RMS", "a_Peak", "a_RMS"], codes
