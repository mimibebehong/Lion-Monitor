"""自定义指标插件：加载、校验、计算（马哥 2026-11-03 要的「用户自己加算法」）。

接口我定：插件文件里给 CODE / UNIT / TITLE / PARAMS_SCHEMA + compute(window, sample_rate, params)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lion_monitor.features import metric_plugins, metrics  # noqa: E402

KURTOSIS = """
CODE = "kurtosis"
UNIT = ""
TITLE = "峭度"
DESCRIPTION = "四阶矩，冲击敏感"
PARAMS_SCHEMA = [{"key": "window_s", "label": "计算时长（s）", "kind": "number", "min": 0.05}]


def compute(window, sample_rate, params):
    import numpy as np

    x = np.asarray(window, dtype="float64")
    sigma = float(x.std())
    if sigma <= 0:
        return 0.0
    return float(np.mean((x - x.mean()) ** 4) / sigma**4)
"""


@pytest.fixture()
def plugin_dir(tmp_path, monkeypatch):
    folder = tmp_path / "plugins"
    folder.mkdir()
    monkeypatch.setenv(metric_plugins.PLUGIN_DIR_ENV, str(folder))
    metric_plugins.load_plugins(force=True)
    yield folder
    metric_plugins.load_plugins(force=True)


def _write(folder: Path, name: str, body: str) -> Path:
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_plugin_is_loaded_with_unit_and_schema(plugin_dir):
    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    plugins, errors = metric_plugins.load_plugins(force=True)

    assert errors == []
    plugin = plugins["kurtosis"]
    assert plugin.title == "峭度"
    assert plugin.unit == ""
    assert plugin.params_schema[0]["key"] == "window_s"


def test_plugin_compute_gets_window_and_params(plugin_dir):
    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)

    # 峭度：正态 ≈ 3，方波 = 1
    normal = np.random.default_rng(0).normal(size=4000)
    value = metric_plugins.compute_plugin_value("kurtosis", normal, 1000.0, {"window_s": 2.0})
    assert 2.5 < value < 3.5
    square = np.sign(np.sin(2 * np.pi * 5 * np.arange(4000) / 1000))
    assert metric_plugins.compute_plugin_value("kurtosis", square, 1000.0, {}) == pytest.approx(
        1.0, abs=0.01
    )


def test_bad_plugin_is_reported_not_loaded(plugin_dir):
    _write(plugin_dir, "no_code.py", "def compute(window, sample_rate, params):\n    return 0.0\n")
    _write(plugin_dir, "no_compute.py", 'CODE = "abc"\nUNIT = "g"\n')
    _write(
        plugin_dir,
        "boom.py",
        'CODE = "boom"\n\n\ndef compute(w, s, p):\n    raise RuntimeError("炸了")\n',
    )

    plugins, errors = metric_plugins.load_plugins(force=True)
    assert set(plugins) == {"boom"}
    messages = " ".join(text for _path, text in errors)
    assert "CODE" in messages and "compute" in messages

    with pytest.raises(metric_plugins.PluginError, match="炸了"):
        metric_plugins.compute_plugin_value("boom", np.zeros(10), 100.0, {})


def test_code_cannot_shadow_builtin(plugin_dir):
    _write(plugin_dir, "dup.py", 'CODE = "v_RMS"\n\n\ndef compute(w, s, p):\n    return 1.0\n')
    plugins, errors = metric_plugins.load_plugins(force=True)

    assert "v_RMS" not in plugins
    assert any("内置" in text for _path, text in errors)


def test_available_codes_merge_builtin_and_plugins(plugin_dir):
    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)

    codes = metric_plugins.available_codes()
    assert {"v_RMS", "a_Peak", "a_RMS", "kurtosis"} <= set(codes)
    assert metric_plugins.unit_of("v_RMS") == "mm/s"
    assert metric_plugins.unit_of("kurtosis") == ""


def test_validate_code_known_explains_missing_algorithm(plugin_dir):
    metric_plugins.validate_code_known("v_RMS")  # 内置不抛
    with pytest.raises(metric_plugins.PluginError, match="没有对应算法"):
        metric_plugins.validate_code_known("v_Peak")


def test_hot_reload_picks_up_edited_plugin(plugin_dir):
    path = _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)
    assert metric_plugins.unit_of("kurtosis") == ""

    path.write_text(KURTOSIS.replace('UNIT = ""', 'UNIT = "1"'), encoding="utf-8")
    assert metric_plugins.unit_of("kurtosis") == "1", "改完文件应自动重新加载"


def test_non_finite_result_is_an_error(plugin_dir):
    _write(
        plugin_dir,
        "nan_metric.py",
        'CODE = "nan_metric"\n\n\ndef compute(w, s, p):\n    return float("nan")\n',
    )
    metric_plugins.load_plugins(force=True)
    with pytest.raises(metric_plugins.PluginError, match="非有限"):
        metric_plugins.compute_plugin_value("nan_metric", np.zeros(4), 100.0, {})


def test_metric_series_computes_plugin_code_per_window(plugin_dir):
    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)

    samples = np.sin(2 * np.pi * 50 * np.arange(32000) / 32000)
    series = metrics.compute_metric_series(
        samples, 32000.0, params={"window_s": 2.0, "step_s": 2.0}, codes=["kurtosis"]
    )
    assert series["kurtosis"] == [], "1 s 信号装不满 2 s 窗，不该出点"
    # 4 s 信号 → 2 个窗
    longer = np.sin(2 * np.pi * 50 * np.arange(32000 * 4) / 32000)
    series = metrics.compute_metric_series(
        longer, 32000.0, params={"window_s": 2.0, "step_s": 2.0}, codes=["kurtosis"]
    )
    assert len(series["kurtosis"]) == 2
    assert all(abs(value - 1.5) < 0.6 for _offset, value in series["kurtosis"])


def test_unknown_code_error_mentions_plugin_dir(plugin_dir):
    with pytest.raises(ValueError, match="不认识|没有对应算法"):
        metrics.compute_metric_series(np.zeros(1000), 1000.0, codes=["v_Peak"])


def test_pipeline_skip_reason_accepts_plugin_code(plugin_dir):
    """重算/导入的跳过判断也要认插件码，不能还写着「仅 v_RMS / a_Peak / a_RMS」。"""
    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)

    from lion_monitor.features.pipeline import _skip_reason

    assert _skip_reason("kurtosis", [], None) == ""
    reason = _skip_reason("v_Peak", [], None)
    assert "没有计算实现" in reason and "插件目录" in reason, reason


def test_assign_metric_accepts_plugin_code_without_ledger(plugin_dir):
    """没有台账行也要能绑定插件指标（健康树先建节点、后补定义的场景）。"""
    from sqlalchemy.orm import Session

    from lion_monitor.store import db as store_db
    from lion_monitor.store import metric_assign, tasks, tree

    _write(plugin_dir, "kurtosis.py", KURTOSIS)
    metric_plugins.load_plugins(force=True)

    engine = store_db.make_engine(str(plugin_dir.parent / "assign.db"))
    store_db.init_db(engine)
    with Session(engine) as session:
        task = tasks.create_task(session, name="插件")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "产线", parent_id=root.id)
        component = tree.create_node(session, task.id, "component", "辊压机", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=component.id)
        metric = tree.create_node(
            session, task.id, "metric", "kurtosis", parent_id=channel.id, attrs={"code": "kurtosis"}
        )
        row = metric_assign.assign_metric(session, task.id, metric.id, "kurtosis")
        assert row.code == "kurtosis"
