"""传感器页删除：统一改为二次点击确认（2026-11-03）。

老写法是「勾选框 + 禁用按钮」两个控件；现在与监测对象页 / 数据集页 / 任务页一致：
第一次点删除按钮只是进入待确认（按钮变红「确认删除？」），第二次才真正删除。
切换选中行 / 改变勾选集合后，待确认态必须复位，不能拿上一次的点数误删下一台。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "pages" / "sensors.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import channel_reg, sensors, tasks  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "sen_del.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="删除确认任务")
            for code in ("CH1", "CH2", "CH3"):
                channel_reg.create_channel(session, self.task.id, None, code)
            self.s1 = sensors.create_sensor(
                session,
                self.task.id,
                None,
                model="IEPE 500mV/g",
                channel_code="CH1",
            )
            self.s2 = sensors.create_sensor(
                session,
                self.task.id,
                None,
                model="IEPE 100mV/g",
                channel_code="CH2",
            )
            self.s3 = sensors.create_sensor(
                session,
                self.task.id,
                None,
                model="IEPE 50mV/g",
                channel_code="CH3",
            )

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def sensor_ids(self) -> list[int]:
        with self.session() as session:
            return [row.id for row in sensors.list_sensors(session, self.task.id)]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env, picked: list[int] | None = None):
    import store_ctx
    from streamlit.testing.v1 import AppTest

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(PAGE), default_timeout=180).run()
    if picked is not None:
        at.session_state["sensor_table"] = {"selection": {"rows": picked, "columns": []}}
        at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _labels(at) -> list[str]:
    return [button.label for button in at.button]


def _html(at) -> str:
    return "\n".join(str(block.value) for block in at.markdown)


# ---------------------------------------------------------------- 单台删除


def test_single_delete_has_no_confirm_checkbox(env):
    """老写法（确认复选框）必须彻底移除，只留一个删除按钮。"""
    at = _app(env, [0])

    boxes = [box.label for box in at.checkbox]
    assert not [label for label in boxes if "确认删除这台传感器" in label], boxes
    assert _labels(at).count("删除这台传感器") == 1


def test_single_delete_needs_two_clicks(env):
    at = _app(env, [0])
    first_id = env.sensor_ids()[0]

    at.button(key=f"del_sensor_one_btn_{first_id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    # 第一次点击只进入待确认：按钮变红、文案变「确认删除？」，一台都没删
    assert "确认删除？" in _labels(at), _labels(at)
    assert env.sensor_ids() == [env.s1.id, env.s2.id, env.s3.id]
    html = _html(at)
    assert f"del_sensor_one_btn_{first_id}" in html
    assert "d93a3a" in html.lower()

    at.button(key=f"del_sensor_one_btn_{first_id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert env.sensor_ids() == [env.s2.id, env.s3.id]


def test_single_delete_arming_resets_when_selection_switches(env):
    """在第一台上点过一次后切到第二台：第二台仍要重新点两次，不能一次就删。"""
    at = _app(env, [0])
    first_id, second_id = env.sensor_ids()[:2]

    at.button(key=f"del_sensor_one_btn_{first_id}").click().run()
    assert "确认删除？" in _labels(at)

    at.session_state["sensor_table"] = {"selection": {"rows": [1], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除？" not in _labels(at), _labels(at)

    at.button(key=f"del_sensor_one_btn_{second_id}").click().run()
    assert env.sensor_ids() == [first_id, second_id, env.s3.id], "切换后第一次点击不该删除"

    at.button(key=f"del_sensor_one_btn_{second_id}").click().run()
    assert env.sensor_ids() == [first_id, env.s3.id]


# ---------------------------------------------------------------- 批量删除


def test_batch_delete_has_no_confirm_checkbox(env):
    at = _app(env, [0, 1])

    boxes = [box.label for box in at.checkbox]
    assert not [label for label in boxes if "确认删除选中的" in label], boxes
    assert "删除选中的 2 台" in _labels(at), _labels(at)


def test_batch_delete_keeps_preview_list(env):
    at = _app(env, [0, 1])

    texts = " ".join(str(item.value) for item in at.caption)
    assert "将删除：" in texts
    assert "CH1" in texts and "CH2" in texts


def test_batch_delete_needs_two_clicks(env):
    at = _app(env, [0, 1])

    at.button(key="del_sensor_btn").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除 2 台？" in _labels(at), _labels(at)
    assert len(env.sensor_ids()) == 3, "第一次点击不该真的删除"

    at.button(key="del_sensor_btn").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert env.sensor_ids() == [env.s3.id]


def test_batch_delete_arming_resets_when_selection_changes(env):
    """勾的是 [0,1]，arm 之后改勾 [0,2]：集合一变就复位，仍要再点两次。"""
    at = _app(env, [0, 1])

    at.button(key="del_sensor_btn").click().run()
    assert "确认删除 2 台？" in _labels(at)

    at.session_state["sensor_table"] = {"selection": {"rows": [0, 2], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除 2 台？" not in _labels(at), _labels(at)
    assert "删除选中的 2 台" in _labels(at), _labels(at)

    at.button(key="del_sensor_btn").click().run()
    assert len(env.sensor_ids()) == 3, "勾选集合变了，第一次点击不该删除"
