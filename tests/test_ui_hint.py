"""提示性文字的统一组件（马哥 2026-11-03：提示性文字设计美观点）。

长说明不能塞在窄列里跟按钮抢宽度 —— 用 ui.hint 出一块整行、左边线强调的灰色说明块。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import (  # noqa: E402
    channel_reg,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "hint.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="提示任务")
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

    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "task": task.id}
    store_ctx._session_factory.clear()


def test_hint_renders_note_block(monkeypatch):
    from app import ui

    captured: list[str] = []
    monkeypatch.setattr(ui.st, "markdown", lambda html, **kw: captured.append(str(html)))

    ui.hint("两列都清空 = 删除该行阈值、回到继承。")

    assert len(captured) == 1
    html = captured[0]
    assert "两列都清空" in html
    assert "border-left" in html, html


def test_thresholds_page_shows_explainer_as_hint(env):
    """阈值页的口径说明要用整行提示块，不再塞在保存按钮右侧的窄列里。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "thresholds.py"), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    got = " ".join(str(item.value) for item in at.markdown)
    assert "阈值统一挂在" in got, got[:300]
    assert "border-left" in got, "提示块没走 ui.hint"

    # 页头副标题那种一句话仍用 caption；这条长口径说明必须是 hint（不再出现在 caption 里）
    captions = " ".join(str(item.value) for item in at.caption)
    assert "阈值统一挂在" not in captions, captions
