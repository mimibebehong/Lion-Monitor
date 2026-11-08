"""趋势页 / 评级页 / 工作台对「旧参数值」的展示与手动重算入口（S4A，AppTest 真跑页面）。

口径（``docs/kanban/bodies/s4a_brief.md``）：

- 造两组值（``stale=0`` 与 ``stale=1``）：趋势页能把 stale 点画成灰点 / 空心点，
  图注写「共 N 个点，其中 M 个由旧参数算出」；
- 评级页与工作台显示「待重算 N 条指标值」；
- 趋势页 / 评级页给「重算本节点」按钮，按钮旁写「将重算 N 条、预计耗时」。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "trends.py"
HOME_PAGE = ROOT / "app" / "pages" / "home.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.features.metrics import compute_metric_series  # noqa: E402
from lion_monitor.ingest.vibration import read_vibration  # noqa: E402
from lion_monitor.store import datasets, metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.models import MetricValue  # noqa: E402

FS = 32000.0
FMT = "%Y-%m-%d %H:%M:%S"
STALE_VALUES = (5.0, 6.0, 7.0)  # stale=1 的三个点
FRESH_VALUES = (1.0, 2.0)  # stale=0 的两个点


@pytest.fixture()
def db_with_stale_values(tmp_path, monkeypatch):
    """一个通道节点 + v_RMS：3 个 stale=1、2 个 stale=0 的点。"""
    path = tmp_path / "s4a.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    now = datetime.now().replace(microsecond=0)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="旧参数点", operator="mark")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
        ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric_node = tree.create_node(
            session, task.id, "metric", "v_RMS", parent_id=ch_node.id, attrs={"code": "v_RMS"}
        )
        metric_assign.assign_metric(session, task.id, ch_node.id, "v_RMS")

        folder = tmp_path / "批次01"
        collected = (now - timedelta(days=3)).replace(microsecond=0)
        source = write_dat(
            folder / f"{collected:%Y%m%d%H%M%S}-CH1_Task1.dat",
            samples=(0.5 * np.sin(2 * np.pi * 120 * np.arange(int(FS * 2)) / FS)).astype("float32"),
            channel="CH1",
            started_at=f"{collected:%Y%m%d%H%M%S}",
            sample_rate_hz=FS,
        )
        dataset = datasets.create_dataset(
            session, task.id, "批次01", folder, collected.strftime(FMT)
        )
        channel = datasets.add_file(session, dataset.id, source, "CH1", FS, int(FS * 2))
        base = now - timedelta(days=3)
        for index, value in enumerate(STALE_VALUES + FRESH_VALUES):
            session.add(
                MetricValue(
                    channel_id=channel.id,
                    node_id=ch_node.id,
                    code="v_RMS",
                    value=value,
                    unit="mm/s",
                    level="unset",
                    computed_at=base + timedelta(minutes=2 * index),
                    params_hash="",
                    rpm_source="",
                    stale=1 if value in STALE_VALUES else 0,
                )
            )
        session.flush()
    return {
        "path": path,
        "task_id": task.id,
        "node_id": ch_node.id,
        "metric_node": metric_node.id,
        "source": source,
    }


@pytest.fixture()
def trend_app(db_with_stale_values):
    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()
    assert [exc.value for exc in at.exception] == []
    return at


def _trace(spec: dict, name: str) -> dict:
    for item in spec["data"]:
        if item.get("name") == name:
            return item
    raise AssertionError(f"图上没有名为「{name}」的轨迹：{[d.get('name') for d in spec['data']]}")


def test_trend_page_marks_stale_points_and_caption_counts_them(trend_app):
    captions = "\n".join(item.value for item in trend_app.caption)
    assert "共 5 个点，其中 3 个由旧参数算出" in captions

    spec = json.loads(trend_app.get("plotly_chart")[0].proto.spec)
    stale = _trace(spec, "旧参数（待重算）")
    assert sorted(stale["y"]) == sorted(STALE_VALUES)
    assert stale["marker"]["symbol"] == "circle-open"  # 空心点，与正常实心点区分
    assert stale["marker"]["color"] == "#8b949e"

    fresh = _trace(spec, "v_RMS · 正常点")
    assert sorted(fresh["y"]) == sorted(FRESH_VALUES)


def test_trend_page_has_recompute_button_with_confirm_hint(trend_app):
    """按钮在，且不再用文字预估（马哥 2026-11-03 改成点击后走进度条）。"""
    buttons = [btn for btn in trend_app.button if btn.label == "重算本节点"]
    assert buttons, "趋势页应有「重算本节点」按钮"
    captions = "\n".join(item.value for item in trend_app.caption)
    assert "将重算" not in captions and "预计" not in captions


def test_trend_page_has_no_confirm_checkbox(trend_app):
    """马哥 2026-11-03：确认重算不再用勾选框，改二次点击（与任务页删除一致）。"""
    labels = [box.label for box in trend_app.checkbox]
    assert "确认重算" not in labels, labels


def test_recompute_button_first_click_only_arms(trend_app, db_with_stale_values):
    """第一次点「重算本节点」只进入待确认态，不执行重算。"""
    btn = next(b for b in trend_app.button if b.label == "重算本节点")
    btn.click()
    trend_app.run()
    assert [exc.value for exc in trend_app.exception] == []
    armed = [b.label for b in trend_app.button if b.label == "确认重算？"]
    assert armed, [b.label for b in trend_app.button]
    engine = store_db.make_engine(db_with_stale_values["path"])
    with store_db.make_session_factory(engine)() as session:
        assert metric_store.count_stale(session, db_with_stale_values["task_id"]) == 3  # 仍未重算


def test_trend_page_recompute_button_rewrites_stale_values(trend_app, db_with_stale_values):
    """验收 4：连点两次「重算本节点」→ 该节点该指标 stale 行变 0、值按当前参数更新。"""
    next(btn for btn in trend_app.button if btn.label == "重算本节点").click()
    trend_app.run()  # 第一次：进入待确认
    next(btn for btn in trend_app.button if btn.label == "确认重算？").click()
    trend_app.run()  # 第二次：执行
    assert [exc.value for exc in trend_app.exception] == []

    engine = store_db.make_engine(db_with_stale_values["path"])
    factory = store_db.make_session_factory(engine)
    with factory() as session:
        task_id = db_with_stale_values["task_id"]
        node_id = db_with_stale_values["node_id"]
        assert metric_store.count_stale(session, task_id) == 0
        rows = metric_store.list_values(session, task_id, "CH1", "v_RMS", node_id=node_id)
        values = sorted(round(row.value, 6) for row in rows)
        expected = compute_metric_series(
            read_vibration(db_with_stale_values["source"]).samples, FS
        )["v_RMS"][0][1]
        assert round(expected, 6) in values  # 新值 = 当前生效参数算出来的
        assert all(5.0 != v and 6.0 != v and 7.0 != v for v in values)  # 旧参数值已作废

    assert any("重算完成" in item.value for item in trend_app.success)


def test_rank_tab_shows_pending_stale_count(trend_app):
    """验收 3：评级页的指标卡里显示「待重算 3」条指标值。"""
    metrics = {item.label: item.value for item in trend_app.metric}
    assert metrics["待重算"] == "3"


def test_rank_tab_has_recompute_button_per_node(trend_app):
    """评级页按节点重算：按钮存在，且不再用文字预估（点击后走进度条）。"""
    keys = [btn.key for btn in trend_app.button if btn.label == "重算本节点"]
    assert any(key and key.startswith("rank_recompute") for key in keys), keys
    captions = "\n".join(item.value for item in trend_app.caption)
    assert "将重算" not in captions and "预计" not in captions


def test_home_shows_pending_stale_count(db_with_stale_values):
    """验收 3：工作台显示待重算条数。

    v7（2026-11-03）紧凑化后只留 KPI 卡这一处：卡片值 = 条数，长口径在卡片 ⓘ 悬停里，
    不再额外用一行 caption 复述（马哥「次要信息可适当删除」）。
    """
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(HOME_PAGE), default_timeout=120).run()
    assert [exc.value for exc in at.exception] == []

    metrics = {item.label: item.value for item in at.metric}
    assert metrics["待重算"] == "3"
    # 不再有与 KPI 重复的长 caption
    assert not any("条指标值" in item.value for item in at.caption)


def test_x_axis_defaults_to_beijing_time(trend_app):
    """横坐标默认北京时间：x 是壁钟时间、刻度只到时分秒，轴标题写「北京时间」"""
    spec = json.loads(trend_app.get("plotly_chart")[0].proto.spec)
    line = _trace(spec, "CH1 · v_RMS")
    assert line["x"], "曲线上要有横坐标"
    assert all("T" in str(item) or "-" in str(item) for item in line["x"]), line["x"][:2]
    assert spec["layout"]["xaxis"]["title"]["text"] == "北京时间"
    assert spec["layout"]["xaxis"]["tickformat"] == "%H:%M:%S"


def test_x_axis_can_switch_to_test_point_index(trend_app):
    """切到「测试点数」：x 变成 1,2,3…，hover 仍给北京时间 + 值"""
    mode = next(item for item in trend_app.segmented_control if item.label == "横坐标")
    assert list(mode.options) == ["北京时间", "测试点数"]
    mode.set_value("测试点数").run()
    assert [exc.value for exc in trend_app.exception] == []

    spec = json.loads(trend_app.get("plotly_chart")[0].proto.spec)
    line = _trace(spec, "CH1 · v_RMS")
    assert line["x"] == list(range(1, len(line["x"]) + 1))
    assert spec["layout"]["xaxis"]["title"]["text"] == "测试点数"
    # hover 仍是北京时间（数据点信息不变）
    assert "%{customdata}" in line["hovertemplate"]
    assert all("-" in str(item) and ":" in str(item) for item in line["customdata"])
