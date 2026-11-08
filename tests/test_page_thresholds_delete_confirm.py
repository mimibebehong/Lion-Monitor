"""阈值页删除：统一改为二次点击确认（马哥 2026-11-03）。

老写法是「确认删除该阈值」勾选框 + 禁用按钮；现在与监测对象 / 数据集 / 任务页一致：
第一次点「删除阈值」只进待确认（变红「确认删除？」），第二次才真删。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "thresholds.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    limits,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import MetricLimit  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "th_del.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="阈值删除任务")
        obj = object_registry.create_object(
            session, task.id, "隔膜过辊轴承", "轴承", part="隔膜过辊"
        )
        ch = channel_reg.create_channel(session, task.id, obj.id, "CH1")
        metric_def.seed_defaults(session, task.id)
        defs = {row.code: row for row in metric_def.list_metrics(session, task.id)}
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(
            session,
            task.id,
            "component",
            "隔膜过辊轴承",
            parent_id=device.id,
            attrs={"部位": "隔膜过辊", "转速": "1500"},
            ref_id=obj.id,
        )
        ch_node = tree.create_node(
            session, task.id, "channel", "CH1", parent_id=comp.id, ref_id=ch.id
        )
        for code, warn, alert in (("v_RMS", 4.5, 11.2), ("a_RMS", 2.0, 6.0)):
            node = tree.create_node(
                session,
                task.id,
                "metric",
                code,
                parent_id=ch_node.id,
                attrs={"code": code, "单位": "mm/s"},
                ref_id=defs[code].id,
            )
            session.add(
                MetricLimit(
                    task_id=task.id,
                    node_id=node.id,
                    metric_code=code,
                    warn=warn,
                    alert=alert,
                    direction=">",
                    enabled=1,
                )
            )
        metric_assign.assign_metric(session, task.id, ch_node.id, "v_RMS")
        metric_assign.assign_metric(session, task.id, ch_node.id, "a_RMS")
    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "task_id": task.id}
    store_ctx._session_factory.clear()


def _app(picked: int | None = 0):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    if picked is not None:
        at.session_state["limit_table"] = {"selection": {"rows": [picked], "columns": []}}
        at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _labels(at):
    return [button.label for button in at.button]


def _stored(env):
    with store_db.session_scope(env["factory"]) as session:
        return [row.id for row in limits.list_limits(session, env["task_id"])]


def _html(at):
    return "\n".join(str(block.value) for block in at.markdown)


def test_delete_limit_has_no_confirm_checkbox(env):
    at = _app()

    boxes = [box.label for box in at.checkbox]
    assert not [label for label in boxes if "确认删除该阈值" in label], boxes
    assert _labels(at).count("删除阈值") == 1


def test_delete_limit_needs_two_clicks(env):
    at = _app()
    row_id = _stored(env)[0]

    at.button(key=f"del_limit_btn_{row_id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除？" in _labels(at), _labels(at)
    assert len(_stored(env)) == 2, "第一次点击不该真的删除"
    html = _html(at)
    assert f"del_limit_btn_{row_id}" in html
    assert "d93a3a" in html.lower()

    at.button(key=f"del_limit_btn_{row_id}").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert len(_stored(env)) == 1


def test_delete_limit_arming_resets_when_row_switches(env):
    at = _app()
    first_id, second_id = _stored(env)

    at.button(key=f"del_limit_btn_{first_id}").click().run()
    assert "确认删除？" in _labels(at)

    at.session_state["limit_table"] = {"selection": {"rows": [1], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    assert "确认删除？" not in _labels(at), _labels(at)

    at.button(key=f"del_limit_btn_{second_id}").click().run()
    assert len(_stored(env)) == 2, "切到另一行后第一次点击不该删除"

    at.button(key=f"del_limit_btn_{second_id}").click().run()
    assert _stored(env) == [first_id]
