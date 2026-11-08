"""自定义算法的导入与彻底删除（马哥 2026-11-03）。

- 导入：上传 .py → 校验 CODE/compute → 落盘到插件目录 → 立即可用
- 彻底删除：删掉插件 .py 文件（台账里的指标行不受影响，只是算不出来了 → 由上层拦截）
- 台账删除仍是软删，不动插件文件
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.features import metric_plugins  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_def, tasks  # noqa: E402

GOOD = """
CODE = "crest"
UNIT = ""
TITLE = "峰值因子"
PARAMS_SCHEMA = [{"key": "window_s", "label": "计算时长（s）", "kind": "number"}]


def compute(window, sample_rate, params):
    import numpy as np

    x = np.asarray(window, dtype="float64")
    rms = float(np.sqrt(np.mean(x**2)))
    return 0.0 if rms <= 0 else float(np.max(np.abs(x)) / rms)
"""

NO_COMPUTE = 'CODE = "broken"\nUNIT = ""\nTITLE = "坏插件"\n'
BAD_CODE = """
CODE = "9bad"
UNIT = ""


def compute(window, sample_rate, params):
    return 0.0
"""
BUILTIN_CLASH = """
CODE = "v_RMS"
UNIT = "mm/s"


def compute(window, sample_rate, params):
    return 0.0
"""


@pytest.fixture()
def plugin_dir(tmp_path, monkeypatch):
    folder = tmp_path / "plugins"
    folder.mkdir()
    monkeypatch.setenv(metric_plugins.PLUGIN_DIR_ENV, str(folder))
    metric_plugins.load_plugins(force=True)
    yield folder
    metric_plugins.load_plugins(force=True)


def test_install_plugin_writes_file_and_becomes_available(plugin_dir):
    plugin = metric_plugins.install_plugin("crest.py", GOOD)

    assert plugin.code == "crest"
    assert (plugin_dir / "crest.py").exists()
    plugins, errors = metric_plugins.load_plugins(force=True)
    assert errors == []
    assert "crest" in plugins
    assert metric_plugins.available_codes()["crest"] == ""


@pytest.mark.parametrize(
    ("filename", "source", "fragment"),
    [
        ("broken.py", NO_COMPUTE, "compute"),
        ("bad.py", BAD_CODE, "CODE"),
        ("v_rms.py", BUILTIN_CLASH, "内置"),
        ("notes.txt", GOOD, ".py"),
    ],
)
def test_install_rejects_bad_input_without_writing(plugin_dir, filename, source, fragment):
    with pytest.raises(metric_plugins.PluginError) as excinfo:
        metric_plugins.install_plugin(filename, source)

    assert fragment in str(excinfo.value)
    assert list(plugin_dir.glob("*.py")) == []


def test_install_refuses_overwrite_without_flag(plugin_dir):
    metric_plugins.install_plugin("crest.py", GOOD)

    with pytest.raises(metric_plugins.PluginError) as excinfo:
        metric_plugins.install_plugin("crest.py", GOOD.replace("峰值因子", "峰值因子2"))
    assert "覆盖" in str(excinfo.value)

    plugin = metric_plugins.install_plugin(
        "crest.py", GOOD.replace("峰值因子", "峰值因子2"), overwrite=True
    )
    assert plugin.title == "峰值因子2"


def test_install_rejects_same_code_from_other_file(plugin_dir):
    metric_plugins.install_plugin("crest.py", GOOD)

    with pytest.raises(metric_plugins.PluginError) as excinfo:
        metric_plugins.install_plugin("crest_copy.py", GOOD)
    assert "已被" in str(excinfo.value)
    assert not (plugin_dir / "crest_copy.py").exists()


def test_remove_plugin_deletes_file(plugin_dir):
    metric_plugins.install_plugin("crest.py", GOOD)

    removed = metric_plugins.remove_plugin("crest")

    assert removed == "crest.py"
    assert not (plugin_dir / "crest.py").exists()
    assert "crest" not in metric_plugins.load_plugins(force=True)[0]


def test_remove_plugin_rejects_unknown_code(plugin_dir):
    with pytest.raises(metric_plugins.PluginError):
        metric_plugins.remove_plugin("crest")


def test_ledger_delete_is_soft_and_keeps_plugin_file(plugin_dir, tmp_path, monkeypatch):
    """台账删除只软删定义行，插件文件与算法都还在。"""
    metric_plugins.install_plugin("crest.py", GOOD)

    path = tmp_path / "soft.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="软删任务")
        row = metric_def.create_metric(session, task.id, "crest", unit="")
        metric_def.delete_metric(session, row.id)
        assert [m.code for m in metric_def.list_metrics(session, task.id)] == []
        assert metric_def.code_usage(session, "crest") == []

    assert (plugin_dir / "crest.py").exists()
    assert "crest" in metric_plugins.load_plugins(force=True)[0]


def test_code_usage_lists_tasks_still_using_it(plugin_dir, tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="在用任务")
        metric_def.create_metric(session, task.id, "crest", unit="")
        usage = metric_def.code_usage(session, "crest")

    assert len(usage) == 1
    assert task.code in usage[0] and "在用任务" in usage[0]
