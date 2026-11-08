"""指标配置页的插件交互（马哥 2026-11-03：用户自己新增算法）。

- 自定义算法在页面上可见（码 / 中文名 / 单位 / 参数项 / 文件）
- 「新增指标」的指标码来自可用算法下拉，选中插件会把参数声明带出来
- 台账里能看出某指标是内置 / 插件 / 无算法
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PAGE = ROOT / "app" / "pages" / "metrics.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.features import metric_plugins  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_def, tasks  # noqa: E402

PLUGIN = """
CODE = "kurtosis"
UNIT = ""
TITLE = "峭度"
DESCRIPTION = "对冲击敏感"
PARAMS_SCHEMA = [
    {"key": "window_s", "label": "计算时长（s）", "kind": "number", "min": 0.05},
]


def compute(window, sample_rate, params):
    import numpy as np

    x = np.asarray(window, dtype="float64")
    sigma = float(x.std())
    if sigma <= 0:
        return 0.0
    return float(np.mean((x - x.mean()) ** 4) / sigma**4)
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    folder = tmp_path / "plugins"
    folder.mkdir()
    (folder / "kurtosis.py").write_text(PLUGIN, encoding="utf-8")
    monkeypatch.setenv(metric_plugins.PLUGIN_DIR_ENV, str(folder))

    path = tmp_path / "metrics.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)
    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="插件任务")
        metric_def.seed_defaults(session, task.id)
    metric_plugins.load_plugins(force=True)

    import store_ctx

    store_ctx._session_factory.clear()
    yield {"factory": factory, "task": task.id, "folder": folder}
    store_ctx._session_factory.clear()
    metric_plugins.load_plugins(force=True)


def _app(env):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_PAGE), default_timeout=180).run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def test_plugin_is_listed_on_page(env):
    at = _app(env)
    text = " ".join(str(item.value) for item in at.markdown)
    text += " ".join(str(item.value) for item in at.caption)
    text += " ".join(str(item.label) for item in at.expander)
    assert "自定义算法" in text or any("插件" in str(item.value) for item in at.caption)

    frames = [df.value for df in at.dataframe]
    joined = " ".join(
        frame.to_string() if hasattr(frame, "to_string") else str(frame) for frame in frames
    )
    assert "kurtosis" in joined, joined


def test_new_metric_code_dropdown_offers_plugin(env):
    at = _app(env)
    code_box = next(s for s in at.selectbox if s.label == "指标码")
    options = [str(option) for option in code_box.options]
    assert any(option.startswith("kurtosis") for option in options), options
    assert any(option.startswith("v_RMS") for option in options), options


def test_create_metric_from_plugin_carries_schema(env):
    at = _app(env)
    code_box = next(s for s in at.selectbox if s.label == "指标码")
    target = next(str(o) for o in code_box.options if str(o).startswith("kurtosis"))
    code_box.select(target).run()

    at.button(key="create_metric").click().run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    with env["factory"]() as session:
        rows = {row.code: row for row in metric_def.list_metrics(session, env["task"])}
    assert "kurtosis" in rows, rows
    assert rows["kurtosis"].schema[0]["key"] == "window_s", "插件参数声明应自动带出"
    assert rows["kurtosis"].enabled is True


def test_plugin_table_matches_ledger_style(env):
    """马哥 2026-11-03：插件区要与「指标台账」同风格 —— 左表选中、右详情。"""
    at = _app(env)
    frames = [df.value for df in at.dataframe]
    columns: set[str] = set()
    for frame in frames:
        if hasattr(frame, "columns"):
            columns |= set(map(str, frame.columns))
    assert {"指标码", "中文名", "单位", "参数项"} <= columns, columns

    at.session_state["plugin_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]

    values = [str(item.value) for item in at.text_area]
    assert any('"window_s"' in value for value in values), values
    captions = " ".join(str(item.value) for item in at.caption)
    assert "kurtosis.py" in captions, captions


def test_plugin_area_is_collapsed_right_under_ledger(env):
    """马哥 2026-11-03：插件区折叠，与指标台账/详情并列（收起时不占主界面高度）。"""
    at = _app(env)
    labels = [button.label for button in at.button]
    assert not any("补齐内置指标" in label for label in labels), labels
    # AppTest 不区分折叠态（内容照样渲染），收起行为由浏览器实测覆盖
    assert len(at.expander) == 1, len(at.expander)
    assert "已加载 1 个" in at.expander[0].label, at.expander[0].label

    # 内容始终渲染（AppTest 忽略折叠态），确认按钮与表格都在
    labels = [button.label for button in at.button]
    assert any(label == "扫描算法" for label in labels), labels

    frames = [df.value for df in at.dataframe]
    columns: set[str] = set()
    for frame in frames:
        if hasattr(frame, "columns"):
            columns |= set(map(str, frame.columns))
    assert {"指标码", "中文名", "单位", "参数项"} <= columns, columns


def test_plugin_area_has_import_and_hard_delete(env):
    """马哥 2026-11-03：彻底删算法 + 导入新算法都放在插件区；台账只软删。"""
    at = _app(env)
    labels = [button.label for button in at.button]

    assert any("导入算法" in label for label in labels), labels
    assert any(label == "扫描算法" for label in labels), labels
    assert any(label == "删除算法（.py）" for label in labels), labels

    # 台账侧文案改成「从台账移除」（选中一行才渲染详情卡）
    at.session_state["metric_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    labels = [button.label for button in at.button]
    assert any(label == "从台账移除" for label in labels), labels
    assert not any(label == "删除指标" for label in labels), labels

    at.session_state["plugin_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    labels = [button.label for button in at.button]
    assert any(label == "删除算法（.py）" for label in labels), labels


def test_hard_delete_blocked_while_ledger_uses_code(env):
    """台账还在用这个码时，彻底删除算法要先被拦住并说明原因。"""
    from lion_monitor.store import metric_def

    with store_db.session_scope(env["factory"]) as session:
        metric_def.create_metric(session, env["task"], "kurtosis", unit="")

    at = _app(env)
    at.session_state["plugin_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()
    next(button for button in at.button if "删除算法" in button.label).click().run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    errors = " ".join(str(item.value) for item in at.error)
    assert "先" in errors and "kurtosis" in errors, errors
    assert (env["folder"] / "kurtosis.py").exists()
