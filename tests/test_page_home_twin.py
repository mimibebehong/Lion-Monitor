"""工作台「设备健康状态」改数字孪生卡片（2026-11-04）。

现场三条要求对应的可自动化验的口径：

1. **布局有重叠** —— 九步条原来是 9 个 `st.container` 式卡片（手写 div + `st.columns`），
   在紧凑间距下容器算 66px、实际只分配 57px，压到下面 KPI 的 9px。改成**一整块 HTML 流程条**
   （单 `st.markdown`、固定行高、flex 均分），高度由自身撑开，不再靠 Streamlit 逐块布局。
2. **按监测对象列健康状态、尽量数字孪生** —— 健康区不再是一张「设备」维度表格，
   改成「设备分组 → 每个监测对象一张卡」：对象名 + 类型 + 状态色 + 名下测点（通道）状态点
   + 指标数/超限数/待重算。数据由 `overview.object_overviews()` 提供。
3. **呈现信息可调整** —— 表格里「对象数/通道数/指标数/超限指标」四列在对象卡上变成
   「测点 + 指标 6 · 超限 0 · 待重算 0」一行，不再单独占四列。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "pages" / "home.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.features import overview  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402


def _plain(markup: str) -> str:
    """把渲染出来的 HTML 去掉标签、反转义，只留用户能看到的文字（断言排版用）。"""
    import html as _html
    import re as _re

    return " ".join(_html.unescape(_re.sub(r"<[^>]+>", " ", markup)).split())


class Env:
    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "twin.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        with self.session() as session:
            self.task = tasks.create_task(session, name="孪生")
            root = tree.root_node(session, self.task.id)
            self.device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            self.bearing = tree.create_node(
                session,
                self.task.id,
                "component",
                "隔膜过辊轴承",
                parent_id=self.device.id,
                attrs={"object_type": "轴承"},
            )
            self.motor = tree.create_node(
                session,
                self.task.id,
                "component",
                "入片牵引电机",
                parent_id=self.device.id,
                attrs={"object_type": "电机"},
            )
            for parent, names in ((self.bearing, ("CH1", "CH2")), (self.motor, ("CH3",))):
                for name in names:
                    channel = tree.create_node(
                        session, self.task.id, "channel", name, parent_id=parent.id
                    )
                    tree.create_node(session, self.task.id, "metric", "v_RMS", parent_id=channel.id)

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


# ---------------------------------------------------------------- 数据层


def test_object_overviews_lists_each_monitor_object(env):
    """按监测对象出卡片：对象名、所属设备、名下测点及其状态、指标数与超限数。"""
    with env.session() as session:
        found = overview.object_overviews(session, env.task.id)

    assert [item.name for item in found] == ["隔膜过辊轴承", "入片牵引电机"], found
    bearing, motor = found
    assert bearing.device_name == "卷绕产线"
    assert bearing.type_label == "轴承"
    assert [ch.name for ch in bearing.channels] == ["CH1", "CH2"]
    assert bearing.channel_count == 2
    assert bearing.metric_count == 2
    assert bearing.over_metric_count == 0
    assert motor.metric_count == 1
    # 没数据 → 一律未设阈值 / 未判级，不假装正常
    assert bearing.state == "unset"
    assert {ch.state for ch in bearing.channels} == {"unset"}


def test_object_overviews_reads_state_from_health_map(env):
    """传入 health 时用它的状态（不重复算），供工作台一次算完整页复用。"""
    with env.session() as session:
        nodes = tree.list_nodes(session, env.task.id)
        ch1 = next(item for item in nodes if item.name == "CH1")
        from lion_monitor.features.health import NodeHealth

        metric_node = next(
            item for item in nodes if item.node_type == "metric" and item.parent_id == ch1.id
        )
        health = {item.id: NodeHealth(node_id=item.id) for item in nodes}
        health[ch1.id] = NodeHealth(node_id=ch1.id, state="alert", over_count=3, stale_count=2)
        health[metric_node.id] = NodeHealth(node_id=metric_node.id, state="alert")
        # node_health 会向上汇总：对象的 stale_count 是子树之和，这里照着真值传
        health[env.bearing.id] = NodeHealth(node_id=env.bearing.id, state="alert", stale_count=2)
        found = overview.object_overviews(session, env.task.id, health=health)

    bearing = found[0]
    assert bearing.state == "alert"
    assert bearing.over_metric_count == 1
    assert bearing.stale_count == 2
    assert {ch.name: ch.state for ch in bearing.channels}["CH1"] == "alert"


# ---------------------------------------------------------------- 布局层


def test_stepper_is_one_html_strip_not_nine_blocks():
    """九步条必须是一整块 HTML（单次 markdown），否则逐块布局会压到下面的 KPI。"""
    src = (ROOT / "app" / "ui.py").read_text(encoding="utf-8")
    start = src.index("def stepper(")
    end = src.index("#: 状态 → (色值, 文案)")
    block = src[start:end]

    assert "st.columns(" not in block, "九步条还在用 st.columns 逐块排版"
    assert block.count("markdown(") == 1, "九步条应只输出一块 HTML"
    assert "display:flex" in block.replace(" ", "").lower()
    # 固定行高，避免内容撑高后与下一区块重叠
    assert "height:38px" in block.replace(" ", ""), block


def test_home_health_section_is_twin_cards_not_table():
    """健康区不再用 dataframe 表格，改对象卡（twin-card）。"""
    src = PAGE.read_text(encoding="utf-8")
    start = src.index('section("设备健康状态")')
    end = src.index("left, right = st.columns(")
    block = src[start:end]

    assert "st.dataframe(" not in block, "健康区还在用表格"
    assert "twin_cards(" in block, block
    ui_src = (ROOT / "app" / "ui.py").read_text(encoding="utf-8")
    assert "twin-card" in ui_src, "孪生卡片渲染函数不存在"


def test_twin_card_maps_state_to_color_and_label():
    """状态 → 色值/文案必须与全站健康口径一致（ok/warn/alert/invalid/unset）。"""
    from ui import STATE_STYLE, twin_cards

    assert STATE_STYLE["ok"][0] == "#4caf50"
    assert STATE_STYLE["warn"][0] == "#fb8c00"
    assert STATE_STYLE["alert"][0] == "#d93a3a"
    assert "未设阈值" in STATE_STYLE["unset"][1]

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="隔膜过辊轴承",
        type_label="轴承",
        state="warn",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="alert")],
        metric_count=3,
        over_metric_count=1,
        stale_count=2,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    html = captured[0]
    assert "twin-card" in html
    assert "--c:#fb8c00" in html  # 对象卡片用 warn 色
    assert "--pc:#6b7684" in html  # 测点徽标统一常态灰点（2026-11-04 去红）
    assert "🟠 预警" in html
    assert "指标 3 · 超限 1 · 待 2" in html


def test_home_renders_twin_card_with_object_channels_and_state(env):
    """页面实际渲染出对象名、测点、状态徽标。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(PAGE), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    html = "\n".join(str(item.value) for item in at.markdown)
    assert "twin-card" in html, html[:400]
    assert "隔膜过辊轴承" in html
    assert "CH1" in html and "CH2" in html
    assert "卷绕产线" in html
    # 状态文案沿用全站口径
    assert "未设阈值" in html or "正常" in html


# ---------------------------------------------------------------- 对象图（2026-11-04）


def test_object_images_exist_for_all_four_objects():
    """四个监测对象各要有示意图 + 实拍图（webp，已压到 20KB 以内便于内嵌）。"""
    from ui import OBJECT_ASSET_DIR

    slugs = ("diaphragm_roller", "infeed_motor", "winding_cylinder", "transfer_motor")
    for slug in slugs:
        for kind in ("diagram", "photo"):
            path = OBJECT_ASSET_DIR / f"{slug}_{kind}.webp"
            assert path.exists(), f"缺少对象图 {path.name}"
            assert path.stat().st_size < 20 * 1024, f"{path.name} 太大（会拖慢每次重跑）"


def test_object_name_maps_to_image_slug():
    from ui import object_slug

    assert object_slug("隔膜过辊轴承") == "diaphragm_roller"
    assert object_slug("入片牵引电机") == "infeed_motor"
    assert object_slug("卷绕张力气缸") == "winding_cylinder"
    assert object_slug("移栽电机") == "transfer_motor"
    assert object_slug("没有图的对象") == ""


def test_twin_card_embeds_figure_and_hover_photo():
    from ui import twin_cards

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="隔膜过辊轴承",
        type_label="轴承",
        state="ok",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="ok")],
        metric_count=3,
        over_metric_count=0,
        stale_count=0,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    html = captured[0]
    assert "twin-fig" in html
    assert "data:image/webp;base64," in html
    assert "twin-img dg" in html and "twin-img ph" in html  # 示意图 + 悬停实拍
    # 状态徽标浮在图区右上角（数字孪生观感），不再占文字行
    fig = html[html.index("<div class='twin-fig'") : html.index("<div class='twin-top'")]
    assert "twin-badge" in fig


def test_twin_card_without_image_degrades_to_text():
    from ui import twin_cards

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="没有图的对象",
        type_label="",
        state="unset",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="unset")],
        metric_count=0,
        over_metric_count=0,
        stale_count=0,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    html = captured[0]
    assert "twin-card" in html
    assert "twin-fig" not in html.replace(".twin-fig", "")  # 无匹配图 → 不插图区
    assert "twin-badge inline" in html  # 但状态徽标不能丢，退回行内右对齐


# ---------------------------------------------------------------- 健康度（2026-11-04）


def test_twin_card_shows_health_score_not_raw_metric():
    """卡片显示健康度（值/阈值 均值），不再显示某个指标的原始读数。"""
    from ui import twin_cards

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="隔膜过辊轴承",
        type_label="轴承",
        state="warn",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="warn")],
        metric_count=3,
        over_metric_count=1,
        stale_count=0,
        worst_reading="v_RMS 2.3100 mm/s",
        health_score=0.625,
        health_points=5,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    html = captured[0]
    plain = _plain(html)
    assert "健康度" in plain and "0.62" in plain and "5 点" in plain
    assert "v_RMS 2.3100 mm/s" not in plain  # 不再显示原始指标读数
    assert "mean(指标值 / 阈值)" in html  # 口径进悬停


def test_twin_card_health_score_dash_when_unavailable():
    from ui import twin_cards

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="隔膜过辊轴承",
        type_label="轴承",
        state="unset",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="unset")],
        metric_count=3,
        over_metric_count=0,
        stale_count=0,
        health_score=None,
        health_points=0,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    plain = _plain(captured[0])
    assert "健康度 —" in plain
    assert "无可用点" not in plain  # 长解释只在悬停（title）里，不占正文


def test_twin_card_tiny_health_score_shows_less_than():
    """比值远小于 0.01 时显示「<0.01」，不显示成 0.00。"""
    from ui import twin_cards

    from lion_monitor.features.overview import ChannelView, ObjectView

    obj = ObjectView(
        node_id=1,
        name="隔膜过辊轴承",
        type_label="轴承",
        state="ok",
        device_id=9,
        device_name="卷绕产线",
        channels=[ChannelView(node_id=2, name="CH1", state="ok")],
        metric_count=3,
        over_metric_count=0,
        stale_count=0,
        health_score=0.0012,
        health_points=5,
    )
    captured: list[str] = []
    original = twin_cards.__globals__["st"]
    twin_cards.__globals__["st"] = type(
        "S", (), {"markdown": staticmethod(lambda body, **kw: captured.append(body))}
    )
    try:
        twin_cards([obj], [])
    finally:
        twin_cards.__globals__["st"] = original

    assert "健康度 <0.01" in _plain(captured[0])


def test_twin_card_border_turns_state_color_when_warn_or_alert():
    """2026-11-04：预警 / 告警时卡片外框整体变色，不只是左侧色条。"""

    from ui import TWIN_CSS

    src = Path(__file__).resolve().parents[1] / "app" / "ui.py"
    code = src.read_text(encoding="utf-8")

    # 卡片带状态 class
    assert "twin-card st-{obj.state}" in code
    # 两种状态都整框换色
    assert ".twin-card.st-warn{border-color:#fb8c00" in TWIN_CSS
    assert ".twin-card.st-alert{border-color:#d93a3a" in TWIN_CSS


def test_twin_state_labels_are_three_levels():
    """状态文案三级：健康 / 预警 / 告警。"""
    from ui import STATE_STYLE

    assert STATE_STYLE["ok"][1] == "🟢 健康"
    assert STATE_STYLE["warn"][1] == "🟠 预警"
    assert STATE_STYLE["alert"][1] == "🔴 告警"


def test_point_dot_and_health_number_use_plain_color():
    """2026-11-04：测点徽标红点、健康度 1.00 的红色都恢复正常色。

    状态仍由卡片外框（st-warn / st-alert）与图上徽标表达。
    """
    from ui import _health_html, _point_html

    ch = SimpleNamespace(name="CH2", state="alert")
    html = _point_html(ch)
    assert "--pc:#6b7684" in html, "测点圆点不该再按状态染色"

    obj = SimpleNamespace(health_score=1.0, health_points=5)
    health = _health_html(obj)
    assert "color:#d93a3a" not in health, "健康度数字不该再按 1.0 染红"
    assert "color:#fb8c00" not in health, "健康度数字不该再按 0.8 染橙"
