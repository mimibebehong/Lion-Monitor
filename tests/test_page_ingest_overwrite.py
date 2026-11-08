"""G9 回归：覆盖导入（同目录重导）没有二次确认。

缺陷背景（docs/reviews/2026-10-31-总体复查.md · G9；probes D2）：

- 数据接入页选中「已处理」批次重导时只有一句 ``st.info`` 说明「会先删后写」
- 点一下按钮就按新参数覆盖旧指标值，且不可撤销

口径（本批定）：覆盖前必须有 checkbox 确认，并提示「将覆盖 N 行指标值」；
未勾选时导入按钮不可点。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

ROOT = Path(__file__).resolve().parents[1]
INGEST_PAGE = ROOT / "app" / "pages" / "ingest.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """一个已经导入过两通道批次、且已有 4 行指标值的库。"""
    path = tmp_path / "ingest.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    folder = tmp_path / "四通道保存" / STAMP
    samples = np.sin(2 * np.pi * 100 * np.arange(1000) / FS).astype("float32")
    for ch in (1, 2):
        write_dat(
            folder / f"{STAMP}-CH{ch}_Task1.dat",
            samples=samples,
            channel=f"CH{ch}",
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务G9")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        for ch in (1, 2):
            node = tree.create_node(session, task.id, "channel", f"CH{ch}", parent_id=comp.id)
            for code in ("v_RMS", "a_Peak"):
                tree.create_node(
                    session, task.id, "metric", code, parent_id=node.id, attrs={"code": code}
                )
        row = datasets.import_from_dir(session, task.id, folder)
        for file in datasets.list_files(session, row.id):
            metric_store.upsert_values(
                session, file.id, {"v_RMS": 1.0, "a_Peak": 0.5}, {}, row.collected_at
            )

    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存要按本次库路径重建）

    store_ctx._session_factory.clear()
    yield {"engine": engine, "task": task, "folder": folder, "dataset": row}
    store_ctx._session_factory.clear()


def _batch_entry(env) -> dict:
    return {
        "批次": env["folder"].name,
        "状态": f"已处理 · {env['dataset'].code}",
        "_已处理": True,
        "目录": str(env["folder"]),
        "采集时间": "2026-10-29 15:28:34",
        "通道": "CH1 / CH2",
        "点数": 1000,
        "采样率": FS,
        "时长(s)": 0.03,
        "文件数": 2,
    }


def _run(env, picked: list[str]):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(INGEST_PAGE), default_timeout=180)
    at.session_state["current_task_id"] = env["task"].id
    at.session_state["scan_batches"] = [_batch_entry(env)]
    at.session_state["scan_errors"] = []
    at.session_state["ingest_pick"] = list(picked)
    at.run()
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    return at


def _button(at, label: str):
    for element in at.button:
        if element.label == label:
            return element
    raise AssertionError(f"找不到按钮 {label}：{[b.label for b in at.button]}")


def _overwrite_box(at):
    for element in at.checkbox:
        if "覆盖" in element.label:
            return element
    raise AssertionError(f"找不到覆盖确认勾选框：{[c.label for c in at.checkbox]}")


def test_overwrite_requires_checkbox_and_shows_row_count(env):
    """选中已处理批次：显示「将覆盖 4 行指标值」的确认框，未勾选时按钮不可点。"""
    at = _run(env, [env["folder"].name])

    box = _overwrite_box(at)
    assert "将覆盖 4 行指标值" in box.label, box.label
    assert box.value is False

    button = _button(at, "导入选中批次（1）")
    assert button.disabled is True

    box.check()
    at.run()
    assert _button(at, "导入选中批次（1）").disabled is False


def test_fresh_batch_has_no_overwrite_checkbox(env):
    """未处理批次（首次导入）不需要覆盖确认，按钮可点。"""
    at = _run(env, [])
    assert not any("覆盖" in element.label for element in at.checkbox)
