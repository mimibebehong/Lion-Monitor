"""G8 页面回归：整批文件头时间都读不了时，页面只给一条中文提示，不打 Traceback。

口径（本卡定）：整批坏头的批次不建数据集，导入结果是 ``discarded=True``；
页面按已有「整批没留下数据集」文案提示（含文件名与「文件头时间不可解析」），
不出现空采集时间箭头（``→ 无效`` 前面不能是空的/``None``）。
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
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store import tasks, tree  # noqa: E402
from lion_monitor.store.models import Channel, Dataset  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"
BAD_STAMP = "20261345256000"
CHANNELS = ("CH1", "CH2", "CH3")


def _signal() -> np.ndarray:
    t = np.arange(int(FS)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "g8-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务页面G8")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        for code in CHANNELS:
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

    import store_ctx  # noqa: E402  （页面用的会话上下文，缓存要按本次库路径重建）

    store_ctx._session_factory.clear()
    yield {"engine": engine, "task": task, "tmp_path": tmp_path}
    store_ctx._session_factory.clear()


def _batch(folder: Path) -> dict:
    return {
        "批次": folder.name,
        "状态": "未处理",
        "_已处理": False,
        "目录": str(folder),
        "采集时间": "2026-10-29 15:28:34",
        "通道": "CH1 / CH2 / CH3",
        "点数": int(FS),
        "采样率": FS,
        "时长(s)": 1.0,
        "文件数": len(CHANNELS),
    }


def _run(env, folder: Path):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(INGEST_PAGE), default_timeout=180)
    at.session_state["current_task_id"] = env["task"].id
    at.session_state["scan_batches"] = [_batch(folder)]
    at.session_state["scan_errors"] = []
    at.run()
    return at


def _import_button(at):
    for element in at.button:
        if element.label.startswith("导入选中批次"):
            return element
    raise AssertionError(f"找不到导入按钮：{[b.label for b in at.button]}")


def _alive_counts(env) -> tuple[int, int]:
    with store_db.make_session_factory(env["engine"])() as session:
        ds = session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count()
        ch = session.query(Channel).filter(Channel.deleted_at.is_(None)).count()
        return ds, ch


def test_all_bad_header_batch_reports_one_chinese_error_without_traceback(env):
    """整批文件头时间都读不了：一条中文提示、无 Traceback、库内 0 残留。"""
    folder = env["tmp_path"] / "四通道保存" / STAMP
    for code in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{code}_Task1.dat",
            samples=_signal(),
            channel=code,
            started_at=BAD_STAMP,
            sample_rate_hz=FS,
        )

    at = _run(env, folder)
    _import_button(at).click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]
    errors = [item.value for item in at.error]
    assert len(errors) == 1, errors
    message = errors[0]
    assert "未保留" in message
    assert "文件头时间不可解析" in message
    assert f"{STAMP}-CH1_Task1.dat" in message
    # 没有采集时间时不能出现空箭头 / None
    assert "None →" not in message
    assert "： →" not in message
    assert "Traceback" not in message and "sqlite" not in message

    assert _alive_counts(env) == (0, 0)
    with store_db.make_session_factory(env["engine"])() as session:
        for code in CHANNELS:
            assert metric_store.list_values(session, env["task"].id, code) == []
