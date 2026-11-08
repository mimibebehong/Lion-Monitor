"""阈值设置页「变更历史」区：把 change_log 按实体过滤、倒序、最多 200 条读出来（T3）。"""

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

HISTORY_COLUMNS = ["时间", "实体", "对象", "字段", "旧值 → 新值", "操作人"]


@pytest.fixture()
def db_with_tree(tmp_path, monkeypatch):
    path = tmp_path / "th_audit.db"
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
        assign = metric_assign.assign_metric(session, task.id, ch_node.id, "v_RMS")
        # 老数据：阈值行直接落库（不经过 set_limit，因此没有「创建」留痕）
        session.add(
            MetricLimit(
                task_id=task.id,
                node_id=metric_node.id,
                metric_code="v_RMS",
                warn=5.0,
                alert=10.0,
                direction=">",
                enabled=1,
            )
        )
        session.commit()
        # 验收 1：阈值 5/10 → 6/12（warn / alert 各一条留痕）；验收 3：参数覆盖一条
        limits.set_limit(session, task.id, metric_node.id, "v_RMS", 6.0, 12.0)
        metric_assign.update_params(session, assign.id, {"band_high": 500.0})
    return {"path": path, "factory": factory, "task_id": task.id}


def _run_page():
    import store_ctx  # noqa: E402  （页面用的会话上下文缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    return AppTest.from_file(str(APP_PAGE), default_timeout=120).run()


def _history_frame(at):
    for element in at.dataframe:
        columns = list(element.value.columns)
        if "旧值 → 新值" in columns:
            return element.value
    raise AssertionError(f"页面上没找到「变更历史」表：{at.dataframe}")


def test_change_history_lists_threshold_and_param_changes(db_with_tree):
    at = _run_page()
    assert [exc.value for exc in at.exception] == []

    frame = _history_frame(at)

    assert list(frame.columns) == HISTORY_COLUMNS
    assert len(frame) == 3
    # 倒序：最新的一条（参数覆盖）在最上面
    assert frame.iloc[0]["实体"].startswith("指标参数 #")
    assert frame.iloc[0]["字段"] == "params_json"
    assert '"band_high": 500.0' in str(frame.iloc[0]["旧值 → 新值"])
    # 阈值两条：warn 5 → 6、alert 10 → 12
    assert set(frame["旧值 → 新值"].tolist()[1:]) == {"5 → 6", "10 → 12"}
    assert {value.split(" #")[0] for value in frame["实体"].tolist()[1:]} == {"阈值"}
    assert set(frame["对象"].tolist()[1:]) == {"CH1 · v_RMS"}
    assert set(frame["操作人"]) == {"mark"}


def test_change_history_filters_by_entity_type(db_with_tree):
    at = _run_page()
    at.selectbox(key="chg_entity").set_value("指标参数").run()
    assert [exc.value for exc in at.exception] == []

    frame = _history_frame(at)

    assert len(frame) == 1
    assert frame.iloc[0]["实体"].startswith("指标参数 #")
    assert frame.iloc[0]["字段"] == "params_json"
