"""G4 回归：阈值页的对象列表必须来自监测对象台账（object_registry），不是旧树模型。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G4 / app/pages/thresholds.py:43）：
阈值页还在用旧 ``objects.list_objects``（直接扫健康树里的 component 节点），
所以「监测对象」页（台账 monitor_object）里删掉的对象、或树里遗留的无台账节点，
两边列表不一致。口径（P4-2 任务书）：改读 ``object_registry``，两边逐项一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PAGE = ROOT / "app" / "pages" / "thresholds.py"
OBJECTS_PAGE = ROOT / "app" / "pages" / "objects.py"
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
    """台账 1 个对象（树里装配了通道 + 指标）；树里另有 1 个旧模型遗留组件（无台账）。"""
    path = tmp_path / "g4-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务台账一致")
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
            attrs={"部位": "隔膜过辊"},
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

        # 旧模型遗留：树里的组件节点没有任何台账主数据（旧 objects.list_objects 会列出它）
        legacy = tree.create_node(
            session,
            task.id,
            "component",
            "遗留无台账对象",
            parent_id=device.id,
            attrs={"部位": "老数据"},
        )
        legacy_ch = tree.create_node(session, task.id, "channel", "CH9", parent_id=legacy.id)
        tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=legacy_ch.id,
            attrs={"code": "v_RMS", "单位": "mm/s"},
        )

    import store_ctx  # noqa: E402  （页面用的会话上下文缓存要按本次库路径重建）

    store_ctx._session_factory.clear()
    yield {"engine": engine, "task": task, "factory": factory}
    store_ctx._session_factory.clear()


def _run(page: Path):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(page), default_timeout=180)
    at.run()
    return at


def _registry(env):
    with store_db.session_scope(env["factory"]) as session:
        return [
            (row.name, row.part) for row in object_registry.list_objects(session, env["task"].id)
        ]


def test_thresholds_object_dropdown_equals_registry(env):
    """阈值页对象下拉 == 台账列表（名称+部位）；树里遗留的无台账组件不出现。"""
    at = _run(THRESHOLDS_PAGE)
    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]

    options = at.selectbox(key="lim_obj").options
    assert options == ["全部", "隔膜过辊轴承"]
    assert "遗留无台账对象" not in options

    frame = at.dataframe[0].value
    assert list(frame["监测对象"]) == ["隔膜过辊轴承"]
    assert list(frame["部位"]) == ["隔膜过辊"]
    assert list(frame["通道"]) == ["CH1"]


def test_thresholds_and_registry_page_lists_match(env):
    """同一库状态下，阈值页的对象下拉（去掉「全部」）与监测对象页的台账表逐项一致。"""
    at = _run(THRESHOLDS_PAGE)
    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    picks = [name for name in at.selectbox(key="lim_obj").options if name != "全部"]

    page = _run(OBJECTS_PAGE)
    assert [exc.value for exc in page.exception] == [], [e.value for e in page.exception]
    table = page.dataframe[0].value
    registry_table = [(row["名称"], row["部位"]) for _, row in table.iterrows()]

    assert registry_table == _registry(env) == [("隔膜过辊轴承", "隔膜过辊")]
    assert picks == [name for name, _ in registry_table]


def test_thresholds_rows_none_when_registry_object_not_assembled(env):
    """台账里有、但健康树还没装配的对象：下拉里能选，表格不出行（不冒充其它对象的数据）。"""
    with store_db.session_scope(env["factory"]) as session:
        object_registry.create_object(
            session, env["task"].id, "入片牵引电机", "电机", part="入片牵引"
        )

    at = _run(THRESHOLDS_PAGE)
    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    assert at.selectbox(key="lim_obj").options == ["全部", "隔膜过辊轴承", "入片牵引电机"]

    at.selectbox(key="lim_obj").select("入片牵引电机").run()
    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    # 没装配的台账对象名下没有任何通道：表格只剩空态，不会把 CH1 也算进来
    captions = " ".join(str(item.value) for item in at.caption)
    assert "当前筛选没有可显示的通道" in captions
