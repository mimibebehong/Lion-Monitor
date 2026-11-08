"""按钮配色全局口径（2026-11-03 定，2026-11-03 扩到全局）：

- 正常态一律不加配色（secondary），只有悬停 / 按下 / 键盘聚焦变蓝
- 破坏性确认态用红，由各页自己的作用域 CSS 覆盖
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from lion_monitor.store import (
        channel_reg,
        metric_assign,
        metric_def,
        object_registry,
        tasks,
        tree,
    )
    from lion_monitor.store import db as store_db

    path = tmp_path / "btn.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="按钮任务")
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


def test_button_style_css_declares_hover_only():
    from app import ui

    css = ui.BUTTON_STYLE_CSS
    assert "#4c9be8" in css
    assert ":hover" in css
    assert ":not(:disabled)" in css, "禁用态不该变蓝"
    assert "button {" not in css, "不允许对按钮常态整体上色"


def test_hover_css_is_global(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "thresholds.py"), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    css = " ".join(str(item.value) for item in at.markdown)
    assert "#4c9be8" in css, "缺少悬停变蓝的全局样式"
    assert ":hover" in css, css[:200]


def test_no_primary_buttons_in_pages():
    """页面里的按钮不许写 type="primary"（传递选择的树高亮除外）。"""
    offenders: list[str] = []
    for path in sorted((ROOT / "app" / "pages").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 'type="primary"' in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    for name in ("metric_params_ui.py",):
        path = ROOT / "app" / name
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 'type="primary"' in line:
                offenders.append(f"{name}:{lineno}: {line.strip()}")

    assert offenders == [], "\n".join(offenders)


def test_thresholds_save_button_is_secondary(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "thresholds.py"), default_timeout=180).run()
    save = at.button(key="lim_save")
    assert save.proto.type != "primary", save.proto.type
