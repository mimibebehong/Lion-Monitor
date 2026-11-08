"""健康树页的模板区：导出勾选「包含阈值配置」真渲染（AppTest 跑页面）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "health_tree.py"
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


@pytest.fixture()
def db_with_tree(tmp_path, monkeypatch):
    path = tmp_path / "tpl_page.db"
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
        metric_node = tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=ch_node.id,
            attrs={"code": "v_RMS", "单位": "mm/s"},
            ref_id=defs["v_RMS"].id,
        )
        metric_assign.assign_metric(
            session, task.id, ch_node.id, "v_RMS", params={"band_high": 2000}
        )
        limits.set_limit(session, task.id, metric_node.id, "v_RMS", 1.8, 2.8)
    return path


def test_health_tree_page_renders_export_with_threshold_checkbox(db_with_tree):
    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()

    assert [exc.value for exc in at.exception] == []
    # 批量工具默认收起（低频功能，开关打开才渲染，省掉每次重跑的无谓开销）
    assert "包含阈值配置" not in [item.label for item in at.checkbox]
    at.toggle(key="ht_tools_open").set_value(True).run()
    labels = [item.label for item in at.checkbox]
    assert "包含阈值配置" in labels
    # 默认勾上：模板应带 thresholds 区段
    picked = next(item for item in at.checkbox if item.label == "包含阈值配置")
    assert picked.value is True
    _text = " ".join(str(item.value) for item in at.caption) + " ".join(
        str(item.value) for item in at.markdown
    )
    assert "模板 v2" in _text, _text[:300]

    store_ctx._session_factory.clear()
