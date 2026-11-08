"""G2 页面回归：「数据接入」导入后要提示本批缺了树上哪些通道。

口径（P4-2 任务书）：只提示、不阻断；文案形如
「本批缺少通道：CH4（健康树已装配 4 路，本批 3 路）」。
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
from lion_monitor.store import tasks, tree  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"
TREE_CHANNELS = ("CH1", "CH2", "CH3", "CH4")


def _signal() -> np.ndarray:
    t = np.arange(int(FS * 2)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "g2-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务缺通道")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        for code in TREE_CHANNELS:
            node = tree.create_node(session, task.id, "channel", code, parent_id=comp.id)
            for metric_code in ("v_RMS", "a_Peak"):
                tree.create_node(
                    session,
                    task.id,
                    "metric",
                    metric_code,
                    parent_id=node.id,
                    attrs={"code": metric_code},
                )

    import store_ctx  # noqa: E402  （页面用的会话上下文缓存要按本次库路径重建）

    store_ctx._session_factory.clear()
    yield {"engine": engine, "task": task, "tmp_path": tmp_path}
    store_ctx._session_factory.clear()


def _batch(folder: Path, channels) -> dict:
    return {
        "批次": folder.name,
        "状态": "未处理",
        "_已处理": False,
        "目录": str(folder),
        "采集时间": "2026-10-29 15:28:34",
        "通道": " / ".join(channels),
        "点数": int(FS * 2),
        "采样率": FS,
        "时长(s)": 2.0,
        "文件数": len(channels),
    }


def _batch_dir(tmp_path, channels) -> Path:
    folder = tmp_path / "批次保存" / STAMP
    for ch in channels:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=_signal(),
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )
    return folder


def _import(env, channels) -> str:
    """跑「数据接入」页并点导入，返回导入结果的成功提示文案。"""
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    folder = _batch_dir(env["tmp_path"], channels)
    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(INGEST_PAGE), default_timeout=180)
    at.session_state["current_task_id"] = env["task"].id
    at.session_state["scan_batches"] = [_batch(folder, channels)]
    at.session_state["scan_errors"] = []
    at.run()

    button = next(b for b in at.button if b.label.startswith("导入选中批次"))
    button.click().run()

    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    success = [item.value for item in at.success]
    assert len(success) == 1, success
    return success[0]


def test_batch_missing_channel_shows_chinese_hint(env):
    """树上 4 路、本批 3 个文件：提示缺 CH4，并给出「已装配 4 路 / 本批 3 路」。"""
    summary = _import(env, ("CH1", "CH2", "CH3"))

    assert "本批缺少通道：CH4（健康树已装配 4 路，本批 3 路）" in summary
    # 只提示不阻断：批次状态仍是「有效」
    assert "有效" in summary


def test_batch_all_channels_present_has_no_hint(env):
    """树上 4 路、本批 4 个文件：不提示缺通道。"""
    summary = _import(env, TREE_CHANNELS)

    assert "本批缺少通道" not in summary
