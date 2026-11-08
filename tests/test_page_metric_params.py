"""S2：健康树「监测指标」页签 —— schema 驱动表单的真实 UI 验证（Streamlit AppTest）。

这些都是真跑页面（不是静态断言文案）：改控件 → 点按钮 → 回库查真值。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "health_tree.py"
METRICS_PAGE = ROOT / "app" / "pages" / "metrics.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.params import default_params  # noqa: E402
from lion_monitor.store import (  # noqa: E402
    datasets,
    metric_assign,
    metric_def,
    object_registry,
    tasks,
    tree,
)
from lion_monitor.store import db as store_db  # noqa: E402

FS = 32000.0


class Env:
    """一套夹具：库、任务、通道节点、指标定义、一个带 CH1 的批次。"""

    def __init__(self, tmp_path, monkeypatch):
        path = tmp_path / "ui.db"
        monkeypatch.setenv("LION_DB", str(path))
        self.engine = store_db.make_engine(path)
        store_db.init_db(self.engine)
        factory = store_db.make_session_factory(self.engine)
        with store_db.session_scope(factory) as session:
            self.task = tasks.create_task(session, name="UI 任务")
            root = tree.root_node(session, self.task.id)
            device = tree.create_node(
                session, self.task.id, "device", "卷绕产线", parent_id=root.id
            )
            bearing = object_registry.create_object(session, self.task.id, "3#辊压机", "辊")
            self.object_id = bearing.id
            comp = tree.create_node(
                session,
                self.task.id,
                "component",
                bearing.name,
                parent_id=device.id,
                ref_type="monitor_object",
                ref_id=bearing.id,
            )
            self.channel = tree.create_node(
                session, self.task.id, "channel", "CH1", parent_id=comp.id
            )
            metric_def.seed_defaults(session, self.task.id)
            ds = datasets.create_dataset(
                session, self.task.id, "批次", tmp_path / "b1", "2026-10-31 08:00:00"
            )
            datasets.add_file(session, ds.id, tmp_path / "b1" / "CH1.dat", "CH1", FS, 64000)
            # 一个依赖转速的门控指标（含 scaling=speed 频率项）
            metric_def.create_metric(
                session,
                self.task.id,
                "BPFO_1X",
                "mm/s",
                schema=[
                    {"key": "f0_hz", "label": "特征频率", "group": "basic", "type": "float",
                     "unit": "Hz", "default": 89.6, "scaling": "speed"},
                ],
                defaults={"f0_hz": 89.6},
            )  # fmt: skip

    def bind(self, code: str = "v_RMS", *, assign: bool = True):
        with store_db.session_scope(store_db.make_session_factory(self.engine)) as session:
            node = tree.create_node(
                session,
                self.task.id,
                "metric",
                code,
                parent_id=self.channel.id,
                attrs={"code": code, "单位": "mm/s" if code == "v_RMS" else "g"},
            )
            if assign:
                metric_assign.assign_metric(session, self.task.id, self.channel.id, code)
            return node.id

    def engine_session(self):
        return store_db.session_scope(store_db.make_session_factory(self.engine))

    def metric_node(self, code: str = "v_RMS") -> int:
        with self.engine_session() as session:
            nodes = tree.list_nodes(session, self.task.id)
        return next(
            node.id
            for node in nodes
            if node.node_type == "metric"
            and node.parent_id == self.channel.id
            and ((node.attrs or {}).get("code") or node.name) == code
        )

    def assign_row(self, code: str = "v_RMS"):
        node_id = self.metric_node(code)
        with self.engine_session() as session:
            rows = [
                row
                for row in metric_assign.list_assignments(session, self.task.id)
                if row.node_id == node_id and row.code == code
            ]
            return rows[0] if rows else None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    instance = Env(tmp_path, monkeypatch)
    import store_ctx

    store_ctx._session_factory.clear()
    yield instance
    store_ctx._session_factory.clear()


def _app(env, node_id=None):
    """跑一遍健康树页；默认选中 v_RMS 指标节点（参数编辑入口）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    at.session_state["ht_selected"] = node_id or env.metric_node()
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _expander_labels(at) -> list[str]:
    return [element.label for element in at.get("expander")]


def _number(at, label: str):
    for element in at.number_input:
        if element.label == label:
            return element
    raise AssertionError(f"找不到数值控件 {label}：{[e.label for e in at.number_input]}")


def _button(at, label: str):
    for element in at.button:
        if element.label == label:
            return element
    raise AssertionError(f"找不到按钮 {label}：{[b.label for b in at.button]}")


def _checkbox(at, label: str):
    for element in at.checkbox:
        if element.label == label:
            return element
    raise AssertionError(f"找不到勾选框 {label}：{[c.label for c in at.checkbox]}")


def _select(at, label: str):
    for element in at.selectbox:
        if element.label == label:
            return element
    raise AssertionError(f"找不到下拉 {label}：{[s.label for s in at.selectbox]}")


# ---------------------------------------------------------------- 渲染


def test_channel_tab_is_read_only_summary_and_jumps_to_metric_node(env):
    """通道页签只做汇总：不给参数控件，每行一个「配置参数 →」跳到指标节点（现场口径）。"""
    env.bind()
    at = _app(env, env.channel.id)

    assert "监测指标" in [tab.label for tab in at.tabs]
    assert not [n for n in at.number_input if n.label.startswith("计算时长")], [
        n.label for n in at.number_input
    ]
    captions = " ".join(str(item.value) for item in at.caption)
    assert "窗长" in captions and "步长" in captions, captions

    _button(at, "配置参数 →").click().run()
    assert at.session_state["ht_selected"] == env.metric_node()


def test_metric_node_has_schema_form(env):
    """参数表单搬到指标节点：默认值、高级参数、勾选项都在这里。"""
    env.bind()
    at = _app(env)

    assert "指标参数" in [tab.label for tab in at.tabs]
    labels = _expander_labels(at)
    assert any("v_RMS" in label and "默认值" in label for label in labels), labels
    assert "高级参数（带宽/算法）· 使用默认值" in labels, labels

    assert _number(at, "计算时长（s）").value == 2.0
    assert _number(at, "计算步长（s）").value == 2.0
    assert _number(at, "带通下限（Hz）").value == 10.0
    assert _number(at, "带通上限（Hz）").value == 1000.0
    # 复查 v2 问题 5：默认窗型改成 rect（hanning 会把 10 Hz 带边缘幅值削掉 10.31%）
    assert _select(at, "窗函数").value == "rect"
    assert _checkbox(at, "去直流").value is True


def test_advanced_section_is_collapsed_by_default(env):
    env.bind()
    at = _app(env)
    advanced = next(e for e in at.get("expander") if e.label.startswith("高级参数"))
    assert advanced.proto.expanded is False


@pytest.mark.parametrize("code", ["a_Peak", "a_RMS"])
def test_time_domain_metric_form_shows_window_and_step(env, code):
    """S4W-2 验收 2：时域指标表单里出现「计算时长（s）/计算步长（s）」两项，默认 2 s / 2 s。"""
    env.bind(code)
    at = _app(env, env.metric_node(code))

    assert _number(at, "计算时长（s）").value == 2.0
    assert _number(at, "计算步长（s）").value == 2.0
    # 窗函数只属于频域指标；带通：a_RMS 有（2026-11-02 现场要求），a_Peak 没有
    assert [s.label for s in at.selectbox if s.label == "窗函数"] == []
    bands = [n.label for n in at.number_input if n.label.startswith("带通")]
    assert bands == (["带通下限（Hz）", "带通上限（Hz）"] if code == "a_RMS" else []), bands


def test_unbound_metric_node_offers_bind_button(env):
    """早期/模板建的指标节点没有参数绑定：提示并可一键绑定。"""
    env.bind(assign=False)
    at = _app(env)

    assert any("未绑定参数" in label for label in _expander_labels(at))
    assert "绑定参数" in [b.label for b in at.button]


def test_bind_button_creates_assignment(env):
    env.bind(assign=False)
    at = _app(env)
    _button(at, "绑定参数").click().run()

    assert [exc.value for exc in at.exception] == []
    assert env.assign_row() is not None


# ---------------------------------------------------------------- 已修改 N 项 / 恢复默认


def test_changing_advanced_param_marks_modified_count(env):
    env.bind()
    at = _app(env)
    _number(at, "带通上限（Hz）").set_value(2000.0).run()

    labels = _expander_labels(at)
    assert "高级参数 · 已修改 1 项 🔶" in labels, labels
    assert any("v_RMS" in label and "已修改 1 项" in label for label in labels), labels


def test_reset_one_item_restores_default(env):
    env.bind()
    at = _app(env)
    _number(at, "带通上限（Hz）").set_value(2000.0).run()
    assert "高级参数 · 已修改 1 项 🔶" in _expander_labels(at)

    assign_id = env.assign_row().id
    at.button(key=f"mp_reset_{assign_id}_band_high").click().run()

    assert _number(at, "带通上限（Hz）").value == 1000.0
    assert "高级参数（带宽/算法）· 使用默认值" in _expander_labels(at)


# ---------------------------------------------------------------- 保存 / 校验 / 重算提示


def test_bandwidth_change_asks_confirm_before_saving(env):
    env.bind()
    at = _app(env)
    _number(at, "带通上限（Hz）").set_value(2000.0).run()
    _button(at, "保存参数").click().run()

    warnings = [w.value for w in at.warning]
    assert any("将影响 1 个数据集，需重算" in text for text in warnings), warnings
    # 还没确认：库里没变
    assert env.assign_row().params == {}

    _button(at, "确认保存（需重算）").click().run()

    assert [exc.value for exc in at.exception] == []
    row = env.assign_row()
    assert row.params == {"band_high": 2000.0}
    assert row.params_locked is False


def test_any_param_change_asks_confirm(env):
    """窗函数也算配置参数：改了就要弹重算确认，确认后才落库。"""
    env.bind()
    at = _app(env)
    _select(at, "窗函数").set_value("hamming").run()
    _button(at, "保存参数").click().run()

    warnings = [w.value for w in at.warning]
    assert any("将影响 1 个数据集，需重算" in text for text in warnings), warnings
    assert env.assign_row().params == {}

    _button(at, "确认保存（需重算）").click().run()

    assert [exc.value for exc in at.exception] == []
    assert env.assign_row().params == {"window_fn": "hamming"}


def test_window_change_asks_confirm_before_saving(env):
    """计算时长改动同样影响历史值：必须先弹「影响 N 个数据集，需重算」。"""
    env.bind()
    at = _app(env)
    _number(at, "计算时长（s）").set_value(3.0).run()
    _button(at, "保存参数").click().run()

    warnings = [w.value for w in at.warning]
    assert any("将影响 1 个数据集，需重算" in text for text in warnings), warnings
    assert env.assign_row().params == {}

    _button(at, "确认保存（需重算）").click().run()

    assert [exc.value for exc in at.exception] == []
    assert env.assign_row().params == {"window_s": 3.0}


def test_bulk_bind_all_button_is_removed(env):
    """2026-11-03：新增指标节点已自动建参数卡，页面上的「一键绑定全部指标参数」删掉。"""
    env.bind("v_RMS", assign=False)
    at = _app(env)

    labels = [b.label for b in at.button]
    assert "一键绑定全部指标参数" not in labels, labels
    assert "确认绑定" not in labels, labels
    # 单个指标节点上的「绑定参数」仍保留（就地给这一个节点补卡）
    assert "绑定参数" in labels, labels


def test_validation_blocks_band_above_nyquist(env):
    env.bind()
    at = _app(env)
    _number(at, "带通上限（Hz）").set_value(20000.0).run()
    _button(at, "保存参数").click().run()

    errors = [e.value for e in at.error]
    assert any("奈奎斯特" in text for text in errors), errors
    assert env.assign_row().params == {}


def test_validation_blocks_step_longer_than_window(env):
    env.bind()
    at = _app(env)
    _number(at, "计算步长（s）").set_value(5.0).run()
    _button(at, "保存参数").click().run()

    errors = [e.value for e in at.error]
    assert any("计算步长" in text for text in errors), errors
    assert env.assign_row().params == {}


def test_validation_blocks_window_too_short_for_band_low(env):
    env.bind()
    at = _app(env)
    _number(at, "带通下限（Hz）").set_value(100.0).run()
    _number(at, "计算时长（s）").set_value(0.5).run()
    _button(at, "保存参数").click().run()

    errors = [e.value for e in at.error]
    assert any("计算时长" in text for text in errors), errors
    assert env.assign_row().params == {}


def test_band_low_auto_raised_for_short_window_with_hint(env):
    """Q9：band_low=2 Hz + 窗长 2 s → 自动抬到 5 Hz，界面给提示，落库的是 5 Hz。"""
    env.bind()
    at = _app(env)
    _number(at, "带通下限（Hz）").set_value(2.0)
    _button(at, "保存参数").click().run()

    infos = [item.value for item in at.info]
    assert any("自动抬高" in text for text in infos), infos
    _button(at, "确认保存（需重算）").click().run()

    infos = [item.value for item in at.info]
    assert any("自动抬高" in text for text in infos), infos
    assert env.assign_row().params["band_low"] == 5.0


def test_locked_save_writes_full_snapshot(env):
    env.bind()
    at = _app(env)
    _checkbox(at, "固化全部参数（此后不再跟随指标定义）").check().run()
    _button(at, "保存参数").click().run()

    assert [exc.value for exc in at.exception] == []
    row = env.assign_row()
    assert row.params_locked is True
    assert row.params == default_params("v_RMS")


# ---------------------------------------------------------------- 转速门控


def test_speed_dependent_metric_is_locked_without_rpm(env):
    at = _app(env, env.channel.id)

    options = _select(at, "选择指标（来自台账）").options
    assert any("BPFO_1X" in option and "🔒 需先设置转速" in option for option in options), options


def test_adding_speed_dependent_metric_without_rpm_is_rejected(env):
    at = _app(env, env.channel.id)
    gated = next(
        option for option in _select(at, "选择指标（来自台账）").options if "BPFO_1X" in option
    )
    _select(at, "选择指标（来自台账）").set_value(gated).run()
    _button(at, "添加指标").click().run()

    errors = [e.value for e in at.error]
    assert any("需先设置转速" in text for text in errors), errors
    with env.engine_session() as session:
        codes = [
            node.name
            for node in tree.list_nodes(session, env.task.id)
            if node.node_type == "metric"
        ]
    assert "BPFO_1X" not in codes


def test_object_rated_rpm_unlocks_speed_dependent_metric(env):
    """转速入口在「监测对象」节点：设了对象额定转速，门控指标即可选。"""
    with env.engine_session() as session:
        object_registry.set_rated_rpm(session, env.object_id, 1480.0)

    at = _app(env, env.channel.id)

    assert [exc.value for exc in at.exception] == []
    options = _select(at, "选择指标（来自台账）").options
    assert not any("🔒" in option for option in options), options

    gated = next(option for option in options if "BPFO_1X" in option)
    _select(at, "选择指标（来自台账）").set_value(gated).run()
    _button(at, "添加指标").click().run()

    assert [exc.value for exc in at.exception] == []
    row = env.assign_row("BPFO_1X")
    assert row is not None
    with env.engine_session() as session:
        channel = tree.get_node(session, env.channel.id)
        assert not channel.attrs.get("转速"), "转速不该再存到通道节点上"


def test_bound_speed_metric_warns_when_rpm_missing(env):
    """已绑定的频率指标在没转速时标红「缺少转速，无法计算」。"""
    env.bind("BPFO_1X")
    at = _app(env, env.metric_node("BPFO_1X"))

    labels = _expander_labels(at)
    assert any("BPFO_1X" in label and "缺少转速" in label for label in labels), labels
    assert any("缺少转速，无法计算" in text for text in [e.value for e in at.error])


def test_metrics_page_exposes_schema_and_defaults(env):
    """指标定义页能改「默认值」与「参数声明」（节点跟随定义的入口）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(METRICS_PAGE), default_timeout=180).run()

    assert [exc.value for exc in at.exception] == []
    assert "声明参数" in [element.label for element in at.metric]
    text_areas = [element.label for element in at.text_area]
    assert any("参数声明（JSON）" in label for label in text_areas), text_areas
