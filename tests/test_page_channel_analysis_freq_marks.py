"""CF-2 验收 6：数据分析页标记线接台账（AppTest 真跑页面 + 真算 FFT）。

- FFT / 包络谱的「显示标记线」里可勾选台账特征频率，标记值与
  ``resolve_freq_marks`` 派生值一致；
- 通道没配频率参数时提示「未配置特征频率」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "channel_analysis.py"
for extra in (ROOT / "src", ROOT / "app", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from _dat_fixture import write_dat  # noqa: E402

from lion_monitor.store import (  # noqa: E402
    component_freq,
    datasets,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402

ATTACHMENT = ROOT / "docs" / "examples" / "监测对象_20260309.csv"
FS = 32000.0
POINTS = 64000


def _sine() -> np.ndarray:
    t = np.arange(POINTS) / FS
    return (0.5 * np.sin(2 * np.pi * 145.0 * t)).astype(np.float32)


class Env:
    def __init__(self, tmp_path, monkeypatch, *, with_ledger: bool = True):
        path = tmp_path / "ana.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        self.dat_path = write_dat(
            tmp_path / "batch" / "CH_Chan_1.dat",
            samples=_sine(),
            channel="Chan_1",
            sample_rate_hz=FS,
        )
        with self.session() as session:
            self.task = tasks.create_task(session, name="标记线任务")
            self.obj = object_registry.create_object(
                session, self.task.id, name="主减速器", obj_type="轴承"
            )
            object_registry.set_rated_rpm(session, self.obj.id, 21000.0)
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(session, self.task.id, "device", "主减", parent_id=root.id)
            comp = tree.create_node(
                session,
                self.task.id,
                "component",
                "主减速器",
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=self.obj.id,
            )
            self.channel = tree.create_node(
                session, self.task.id, "channel", "Chan_1", parent_id=comp.id
            )
            dataset = datasets.create_dataset(
                session,
                self.task.id,
                "批次01",
                str(tmp_path / "batch"),
                "2026-11-02 10:00:00",
            )
            datasets.add_file(session, dataset.id, str(self.dat_path), "Chan_1", FS, POINTS)
            if with_ledger:
                component_freq.import_csv(session, self.obj.id, ATTACHMENT)

    def session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def derived_om1(self) -> float:
        with self.session() as session:
            marks = component_freq.resolve_freq_marks(session, self.task.id, "Chan_1")
        mark = next(item for item in marks if (item.part_code, item.freq_kind) == ("Z1_R", "om1"))
        return mark.value_hz


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


@pytest.fixture()
def env_without_ledger(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch, with_ledger=False)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=240).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _button(at, label: str):
    for element in at.button:
        if element.label == label:
            return element
    raise AssertionError(f"找不到按钮 {label}：{[b.label for b in at.button]}")


def _annotations(fig) -> list[str]:
    return [str(ann.text) for ann in fig.layout.annotations]


def test_fft_ledger_marker_equals_derived_value(env):
    at = _app(env)

    options = list(at.multiselect(key="fft_ledger").options)
    assert any("Z1_R" in text and "OM1" in text for text in options), options
    assert not any("未配置特征频率" in element.value for element in at.caption)

    picked = next(text for text in options if "Z1_R" in text and "OM1" in text)
    at.multiselect(key="fft_ledger").set_value([picked])
    _button(at, "频谱分析").click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    derived = env.derived_om1()
    texts = _annotations(at.session_state["fft_result"]["fig"])
    assert f"目标 {derived:g} Hz" in texts, texts
    assert any("Z1_R" in text and "OM1" in text for text in [c.value for c in at.caption])


def test_envelope_tab_also_uses_ledger(env):
    at = _app(env)

    options = list(at.multiselect(key="env_ledger").options)
    picked = next(text for text in options if "Z1_R" in text and "OM1" in text)
    at.multiselect(key="env_ledger").set_value([picked])
    _button(at, "运行包络谱").click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    derived = env.derived_om1()
    texts = _annotations(at.session_state["env_result"]["fig"])
    assert f"目标 {derived:g} Hz" in texts, texts


def test_channel_without_ledger_shows_hint(env_without_ledger):
    at = _app(env_without_ledger)

    captions = [element.value for element in at.caption]
    assert sum("未配置特征频率" in text for text in captions) >= 2, captions
    assert not any(element.key == "fft_ledger" for element in at.multiselect)


def test_node_override_changes_ledger_marker(env):
    """节点手填覆盖后，分析页勾到的就是覆盖值（来源标「节点手填」）。"""
    with env.session() as session:
        component_freq.set_node_freq_override(session, env.channel.id, "Z1_R", "om1", 400.0)

    at = _app(env)
    options = list(at.multiselect(key="fft_ledger").options)
    picked = next(text for text in options if "Z1_R" in text and "OM1" in text)
    assert "400" in picked and "节点手填" in picked, options

    at.multiselect(key="fft_ledger").set_value([picked])
    _button(at, "频谱分析").click()
    at.run()
    assert f"目标 {400:g} Hz" in _annotations(at.session_state["fft_result"]["fig"])


def test_page_injects_compact_css(env):
    """数据分析页必须注入紧凑版式（马哥 2026-11-03：绘图区不滚动就能看见）。"""
    import ui

    at = _app(env)
    css = ui.compact_css()
    markdown_blob = " ".join(m.value for m in at.markdown)
    assert css in markdown_blob, "页面没有注入 ui.compact_css()"
    assert len(at.exception) == 0, at.exception


def test_envelope_tab_has_no_filter_order_control(env):
    """包络谱参数行只剩 带通下限 / 带通上限 / 去均值（马哥 2026-11-03 删「滤波器阶数」）。"""
    at = _app(env)
    labels = [str(getattr(n, "label", "")) for n in at.number_input] + [
        str(getattr(c, "label", "")) for c in at.checkbox
    ]
    assert not any("滤波器阶数" in label for label in labels), labels
    assert not any(label.strip() == "阶数" and "滤波器" in label for label in labels), labels
    assert any("带通下限" in label for label in labels), labels
    assert any("带通上限" in label for label in labels), labels
