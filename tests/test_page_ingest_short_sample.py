"""G12 页面回归：不足一窗的样本在导入结果区直接说明「判无效 + 具体原因」。

马哥 2026-11-02 定：「不足指标计算窗口的数据直接判无效」。
导入结果区要把原因写清（点数 / 窗长 / 采样率 / 所需点数），不能只留一个 0 值。
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

from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"
CHANNEL = "CH1"
POINTS = 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "g12-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务G12页面")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        node = tree.create_node(session, task.id, "channel", CHANNEL, parent_id=comp.id)
        for code in ("v_RMS", "a_Peak"):
            tree.create_node(
                session, task.id, "metric", code, parent_id=node.id, attrs={"code": code}
            )
        metric_assign.assign_metric(session, task.id, node.id, "v_RMS")

    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存要按本次库路径重建）

    store_ctx._session_factory.clear()
    yield {"engine": engine, "task": task, "tmp_path": tmp_path}
    store_ctx._session_factory.clear()


def _short_batch(tmp_path: Path) -> Path:
    """32 点样本（远不足 2 s 窗）的单通道批次目录。"""
    folder = tmp_path / "单通道保存" / STAMP
    t = np.arange(POINTS) / FS
    write_dat(
        folder / f"{STAMP}-{CHANNEL}_Task1.dat",
        samples=np.sin(2 * np.pi * 100 * t).astype("float32"),
        channel=CHANNEL,
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    return folder


def _run(env, folder: Path):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(INGEST_PAGE), default_timeout=180)
    at.session_state["current_task_id"] = env["task"].id
    at.session_state["scan_batches"] = [
        {
            "批次": folder.name,
            "状态": "未处理",
            "_已处理": False,
            "目录": str(folder),
            "采集时间": "2026-10-29 15:28:34",
            "通道": CHANNEL,
            "点数": POINTS,
            "采样率": FS,
            "时长(s)": POINTS / FS,
            "文件数": 1,
        }
    ]
    at.session_state["scan_errors"] = []
    at.run()
    return at


def _import_button(at):
    for element in at.button:
        if element.label.startswith("导入选中批次"):
            return element
    raise AssertionError(f"找不到导入按钮：{[b.label for b in at.button]}")


def test_short_sample_summary_shows_invalid_with_reason(env):
    """32 点样本：页面摘要写明判无效 + 具体原因，库里 0 个指标值。"""
    folder = _short_batch(env["tmp_path"])

    at = _run(env, folder)
    _import_button(at).click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    flashes = [item.value for item in at.success] + [item.value for item in at.error]
    assert flashes, [el.type for el in at]
    summary = "\n".join(flashes)
    for token in ("不足一窗", "32 点", "64000", CHANNEL):
        assert token in summary, summary

    with store_db.make_session_factory(env["engine"])() as session:
        assert metric_store.list_values(session, env["task"].id, CHANNEL) == []
