"""趋势页接线：默认 30 天窗 + 图上抽稀到 ≤ 2000 点（AppTest 真跑页面）。

口径（P3 / Q6）：

- 页面默认时间窗 = 「最近 30 天」，窗口外的老点不进图；
- 窗口内超过 2000 点时图上按 min/max 抽稀（``features.trend_view.chart_points``），
  图上点数 ≤ 2000，且全序列极值一定在图上；
- 导出按钮仍在（CSV 明细取全量 rows，不经过抽稀）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "trends.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, limits, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store.models import MetricValue  # noqa: E402

FS = 32000.0
FMT = "%Y-%m-%d %H:%M:%S"
IN_WINDOW = 2600  # 窗口内点数 > 2000 → 必须抽稀
OUT_WINDOW = 400  # 30 天以前的老点 → 默认窗口下不该出现


@pytest.fixture()
def db_with_series(tmp_path, monkeypatch):
    """一条通道 + 一个指标节点：窗口内 2600 点（含一个 -99 尖峰）+ 窗口外 400 点。"""
    path = tmp_path / "trend_page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    now = datetime.now().replace(microsecond=0)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="趋势页窗口", operator="mark")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "隔膜过辊轴承", parent_id=device.id)
        ch_node = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        metric_node = tree.create_node(
            session,
            task.id,
            "metric",
            "v_RMS",
            parent_id=ch_node.id,
            attrs={"code": "v_RMS", "单位": "mm/s"},
        )
        limits.set_limit(session, task.id, metric_node.id, "v_RMS", 1.8, 2.8)

        dataset = datasets.create_dataset(
            session,
            task.id,
            "批次01",
            "/tmp/lion-trend-page/b1",
            (now - timedelta(days=20)).strftime(FMT),
        )
        channel = datasets.add_file(
            session, dataset.id, "/tmp/lion-trend-page/b1/CH1.dat", "CH1", FS, IN_WINDOW * 512
        )

        def add(moment: datetime, value: float) -> None:
            session.add(
                MetricValue(
                    channel_id=channel.id,
                    node_id=ch_node.id,
                    code="v_RMS",
                    value=value,
                    unit="mm/s",
                    level="unset",
                    computed_at=moment,
                    params_hash="",
                    rpm_source="",
                    stale=0,
                )
            )

        step = timedelta(minutes=10)
        base = now - timedelta(days=20)
        for index in range(IN_WINDOW):
            value = 1.0 + 0.01 * (index % 50)
            if index == IN_WINDOW // 2:
                value = -99.0  # 窗口内唯一尖峰（替换该点，不新增行）
            add(base + step * index, value)
        for index in range(OUT_WINDOW):
            add(now - timedelta(days=45) + step * index, 42.0)  # 全量最大，但窗口外
    return path


def test_trend_page_defaults_to_thirty_day_window_and_decimates(db_with_series):
    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存需按本次库路径重建）
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(APP_PAGE), default_timeout=120).run()

    assert [exc.value for exc in at.exception] == []

    windows = [box for box in at.selectbox if box.label == "时间窗"]
    assert len(windows) == 1
    assert windows[0].value == "最近 30 天"

    captions = "\n".join(item.value for item in at.caption)
    assert f"共 {IN_WINDOW} 个点" in captions
    assert "图上按 min/max 抽稀到 2000 点" in captions

    # 图上点数 ≤ 2000（页面真实的 plotly spec），窗口内尖峰仍在、窗口外最大值不进图
    chart = at.get("plotly_chart")[0]
    spec = json.loads(chart.proto.spec)
    trace = spec["data"][0]
    assert len(trace["y"]) <= 2000
    assert min(trace["y"]) == pytest.approx(-99.0)
    assert max(trace["y"]) < 42.0

    assert len(at.get("download_button")) == 1  # CSV 导出仍在（明细取全量 rows）


def test_export_button_shares_row_with_recompute():
    """马哥 2026-11-04：导出逐点数据 CSV 与「重算本节点」放在同一行。

    实现：_recompute_block 接受 export=(文件名, CSV)，用 [0.40, 0.34, 0.26] 三列渲染 ——
    左列留给提示/进度条、中间导出、右侧重算。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "trends.py").read_text(
        encoding="utf-8"
    )

    assert "export: tuple[str, bytes] | None = None" in src
    assert (
        'st.columns(\n            [0.40, 0.34, 0.26], vertical_alignment="center"\n        )' in src
    )
    assert 'key=f"{key}_export"' in src
    # 导出只在趋势页通过 export 参数传入，图下方不再单独渲染下载按钮
    head, _, _ = src.partition("with tab_rank:")
    assert head.count("st.download_button(") == 1
