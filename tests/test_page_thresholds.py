"""阈值设置页（S-1）：方向下拉按方向校验 —— AppTest 真跑页面。

data_editor 的单元格编辑在 AppTest 里没有直接 API，走 session_state 注入（Streamlit 把
编辑结果就存在 ``st.session_state[EDIT_KEY]`` 的 ``edited_rows`` 里）。
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

from lion_monitor import limits_rules  # noqa: E402
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

EDIT_KEY = "limit_editor"
LOWER = limits_rules.DIRECTION_TEXT["lower"]
UPPER = limits_rules.DIRECTION_TEXT["upper"]


@pytest.fixture()
def db_with_tree(tmp_path, monkeypatch):
    path = tmp_path / "th_page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="卷绕产线振动监测", operator="mark")
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
        tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=ch_node.id,
            attrs={"code": "v_RMS", "单位": "mm/s"},
            ref_id=defs["v_RMS"].id,
        )
        metric_assign.assign_metric(session, task.id, ch_node.id, "v_RMS")
    return {"path": path, "factory": factory, "task_id": task.id}


def _run_page():
    import store_ctx  # noqa: E402  （页面用的会话上下文缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()
    return at


def _set_editor(at, *, warn: str, alert: str, direction: str):
    at.session_state[EDIT_KEY] = {
        "edited_rows": {0: {"warn": warn, "alert": alert, "方向": direction}},
        "added_rows": [],
        "deleted_rows": [],
    }


def _rows(db_with_tree):
    with store_db.session_scope(db_with_tree["factory"]) as session:
        return [
            (row.metric_code, row.warn, row.alert, row.direction)
            for row in limits.list_limits(session, db_with_tree["task_id"])
        ]


def test_lower_direction_warn_3_alert_1_saves_and_reads_back(db_with_tree):
    at = _run_page()
    assert [exc.value for exc in at.exception] == []

    _set_editor(at, warn="3", alert="1", direction=LOWER)
    at.run()
    assert [exc.value for exc in at.exception] == []
    # 选了下限就把该方向的填数口径提示出来
    _text = " ".join(str(c.value) for c in at.caption) + " ".join(
        str(item.value) for item in at.markdown
    )
    assert "warn 填较轻的门槛（数值较大）" in _text, _text[:300]

    at.button(key="lim_save").click().run()

    assert [exc.value for exc in at.exception] == []
    assert [str(e.value) for e in at.error] == []
    assert any("已保存" in str(e.value) for e in at.success)
    assert _rows(db_with_tree) == [("v_RMS", 3.0, 1.0, "<")]
    # 回显：表格重新从库里解析出 3 / 1 / 下限
    frame = at.dataframe[0].value
    assert str(frame.iloc[0]["warn"]) == "3"
    assert str(frame.iloc[0]["alert"]) == "1"
    assert frame.iloc[0]["方向"] == LOWER


def test_lower_direction_warn_1_alert_3_reports_direction_error(db_with_tree):
    at = _run_page()

    _set_editor(at, warn="1", alert="3", direction=LOWER)
    at.run()
    at.button(key="lim_save").click().run()

    assert [exc.value for exc in at.exception] == []
    errors = " ".join(str(e.value) for e in at.error)
    assert "下限" in errors
    assert "warn > alert" in errors
    assert _rows(db_with_tree) == []


def test_upper_direction_warn_4_5_alert_2_8_reports_direction_error(db_with_tree):
    at = _run_page()

    _set_editor(at, warn="4.5", alert="2.8", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()

    assert [exc.value for exc in at.exception] == []
    errors = " ".join(str(e.value) for e in at.error)
    assert "上限" in errors
    assert "warn < alert" in errors
    assert _rows(db_with_tree) == []


def test_legacy_reversed_lower_row_is_flagged_not_silently_changed(db_with_tree):
    """老行 direction="<" 但 warn<alert：页面提示人工确认，库里数值一个都不动。"""
    with store_db.session_scope(db_with_tree["factory"]) as session:
        metric_node = next(
            node
            for node in tree.list_nodes(session, db_with_tree["task_id"])
            if node.node_type == "metric" and node.name == "v_RMS"
        )
        session.add(
            MetricLimit(
                task_id=db_with_tree["task_id"],
                node_id=metric_node.id,
                metric_code="v_RMS",
                warn=0.2,
                alert=0.5,
                direction="<",
                enabled=1,
            )
        )

    at = _run_page()

    assert [exc.value for exc in at.exception] == []
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "人工确认" in warnings
    assert "下限" in warnings
    stored = _rows(db_with_tree)
    assert stored == [("v_RMS", 0.2, 0.5, "<")]


# ---------------------------------------------------------------- 单边设置
# 2026-11-04：可以只设 warn 或只设 alert，留空的一档记 0 = 该档不判。


def test_warn_only_saves_with_alert_zero(db_with_tree):
    at = _run_page()
    assert [exc.value for exc in at.exception] == []

    _set_editor(at, warn="4.5", alert="", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()

    assert [str(e.value) for e in at.error] == []
    assert any("已保存" in str(e.value) for e in at.success)
    assert _rows(db_with_tree) == [("v_RMS", 4.5, 0.0, ">")]


def test_alert_only_saves_with_warn_zero(db_with_tree):
    at = _run_page()

    _set_editor(at, warn="", alert="11.2", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()

    assert [str(e.value) for e in at.error] == []
    assert _rows(db_with_tree) == [("v_RMS", 0.0, 11.2, ">")]


def test_both_blank_deletes_row(db_with_tree):
    at = _run_page()

    _set_editor(at, warn="4.5", alert="11.2", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()
    assert _rows(db_with_tree) == [("v_RMS", 4.5, 11.2, ">")]

    _set_editor(at, warn="", alert="", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()
    assert [str(e.value) for e in at.error] == []
    assert _rows(db_with_tree) == []


def test_unset_side_echoes_blank_not_zero(db_with_tree):
    """未设的一档回显空单元格，别显示成 0（看起来像「阈值 0」）。"""
    at = _run_page()

    _set_editor(at, warn="4.5", alert="", direction=UPPER)
    at.run()
    at.button(key="lim_save").click().run()
    assert [str(e.value) for e in at.error] == []
    at.run()

    frame = at.dataframe[0].value
    assert str(frame.iloc[0]["warn"]) == "4.5"
    assert str(frame.iloc[0]["alert"]) == "", frame.iloc[0]["alert"]
