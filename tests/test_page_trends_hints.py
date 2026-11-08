"""趋势页 / 评级页提示信息精简（马哥 2026-11-03 标准档）。

- 每条曲线不再各占一行「📈 CH · code 共 N 点」，全页合并成一行汇总；
- 「⏳ 待重算 0 条…」在 0 条时不显示，且不再重复出现两次；
- 树的「＋/－ 展开折叠；点节点名称选中」改成角落 ⓘ 的 tooltip，不再占一行；
- 评级页 section 说明精简、空表时不再单独一行提示。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "trends.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import MetricValue  # noqa: E402

FS = 32000.0
FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture()
def two_metric_db(tmp_path, monkeypatch):
    """一个通道挂两个指标（v_RMS / a_Peak），5 个新鲜点，阈值空（不产生超限记录）。"""
    path = tmp_path / "hints.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    now = datetime.now().replace(microsecond=0)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="提示精简", operator="mark")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
        ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        for code in ("v_RMS", "a_Peak"):
            tree.create_node(
                session, task.id, "metric", code, parent_id=ch_node.id, attrs={"code": code}
            )
            metric_assign.assign_metric(session, task.id, ch_node.id, code)

        folder = tmp_path / "批次01"
        collected = (now - timedelta(days=2)).replace(microsecond=0)
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
        base = now - timedelta(days=2)
        for code, unit in (("v_RMS", "mm/s"), ("a_Peak", "g")):
            for index in range(5):
                session.add(
                    MetricValue(
                        channel_id=channel.id,
                        node_id=ch_node.id,
                        code=code,
                        value=1.0 + index,
                        unit=unit,
                        level="unset",
                        computed_at=base + timedelta(minutes=2 * index),
                        params_hash="",
                        rpm_source="",
                        stale=0,
                    )
                )
        session.flush()
    return {"path": path}


@pytest.fixture()
def trend_app(two_metric_db):
    import store_ctx
    from streamlit.testing.v1 import AppTest

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _captions(at) -> list[str]:
    return [item.value for item in at.caption]


def test_per_curve_captions_merged_into_one_line(trend_app):
    """两个指标不再各占一行：全页只留一行「📈 … 条曲线 · 共 N 个点」。"""
    charts = [c for c in _captions(trend_app) if c.startswith("📈")]
    assert len(charts) == 1, charts
    assert "2 条曲线" in charts[0]
    assert "共 5 个点" in charts[0]


def test_zero_stale_does_not_print_recompute_hint(trend_app):
    """0 条待重算时不打印「⏳ 待重算 0 条…」。"""
    assert not [c for c in _captions(trend_app) if "待重算" in c], _captions(trend_app)


def test_tree_hint_moved_into_tooltip(trend_app):
    """树的「＋/－ 展开折叠…」不再作为一行 caption 显示。"""
    assert not [c for c in _captions(trend_app) if "展开折叠" in c], _captions(trend_app)


def test_rank_tab_has_single_recompute_hint(trend_app):
    """评级页的「⏳ 待重算…」最多出现一次，不再两处重复；空表也不再单独一行提示。"""
    assert trend_app.tabs, "页面应有两个页签（趋势 / 健康评级）"
    captions = _captions(trend_app)
    assert len([c for c in captions if "待重算" in c]) <= 1, captions
    assert "当前没有越过 warn 的点" not in captions, captions


def test_header_subtitle_shortened(trend_app):
    """页头副标题精简（page_header 的 subtitle 走 st.caption）。"""
    text = "\n".join(item.value for item in trend_app.caption)
    assert "左选节点" in text
    assert "CSV 导出全量" not in text, "口径不该再塞在页头（已移到时间窗/导出控件 tooltip）"


def test_rank_table_draws_tree_prefixes(trend_app):
    """节点评级表的「节点」列用 ├─/└─ 树形前缀缩进（马哥 2026-11-03：缩进要美观）。"""
    html = "\n".join(item.value for item in trend_app.markdown)
    assert "rank-table" in html, "评级表应改用可控制缩进/对齐的表格"
    assert "├─" in html or "└─" in html, "节点列要用树形前缀缩进"
    assert "　" not in html.split("rank-table")[0][-50:]


def test_rank_table_centers_over_count(trend_app):
    """「超限条数」数字居中显示（马哥 2026-11-03）。"""
    html = "\n".join(item.value for item in trend_app.markdown)
    assert "td.cnt" in html or "class='cnt'" in html or 'class="cnt"' in html
    assert "text-align:center" in html.replace(" ", "")


def test_rank_table_keeps_state_dots(trend_app):
    """状态仍用色点 + 文字（与健康树一致），不是纯文字。"""
    html = "\n".join(item.value for item in trend_app.markdown)
    assert "dot" in html and ("未设阈值" in html or "正常" in html)


def test_kpi_long_notes_are_tooltips_not_captions(trend_app):
    """长口径走 st.metric(help=...)，不能渲染成占版面的 caption。"""
    tips = [m.help for m in trend_app.metric if m.help]
    assert any("最近 5 个点" in (t or "") for t in tips), "严重/警告的口径应挂在悬停提示里"
    assert any("stale" in (t or "") or "不参与判级" in (t or "") for t in tips)
    caps = "\n".join(_captions(trend_app))
    assert "最近 5 点越过 alert" in caps, "卡片下只留一句短说明"
    for long_text in ("跳过待重算的旧参数点", "不参与判级；重算会回读"):
        assert long_text not in caps, "长口径不应出现在可见 caption 里"


def test_no_node_total_kpi(trend_app):
    """马哥 2026-11-03：「节点总数 22」这张卡不用显示。"""
    labels = [item.label for item in trend_app.metric]
    assert "节点总数" not in labels, labels


def test_grade_breakdown_kpi_explains_invalid(trend_app):
    """「数据异常」要有悬停解释：值非有限（NaN/±Inf）无法判级。"""
    card = next(m for m in trend_app.metric if m.label.startswith("正常"))
    assert card.help and "NaN" in card.help and "数据异常" in card.help


def test_no_recompute_estimate_text(trend_app):
    """马哥 2026-11-03：去掉「将重算 360 条，预计 8.4 秒左右」，改成点击时用进度条。"""
    caps = "\n".join(_captions(trend_app))
    assert "将重算" not in caps, caps
    assert "预计" not in caps, caps
