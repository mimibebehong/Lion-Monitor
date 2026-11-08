"""B3 页面回归：数据接入页收到坏输入时只报一条中文提示，不打 Traceback。

口径（本批定）：
- 坏文件（NaN / scale=NaN / 采样率非法）→ 整批降级，页面提示「未保留数据集」，库内 0 残留
- 同批重复通道号 → 预检抛中文 ``DatasetError``，页面一条中文错误，库内 0 残留
- 任何未预料的异常 → 兜底 ``Exception``：中文可操作提示，不暴露 SQL / 堆栈；堆栈进日志
"""

from __future__ import annotations

import logging
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
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


def _signal() -> np.ndarray:
    t = np.arange(int(FS)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "guard-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务页面防线")
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
        "通道": "CH1 / CH2 / CH3 / CH4",
        "点数": int(FS),
        "采样率": FS,
        "时长(s)": 1.0,
        "文件数": 4,
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


def _assert_no_traceback(at) -> None:
    assert [exc.value for exc in at.exception] == [], [exc.value for exc in at.exception]


def _alive_counts(env) -> tuple[int, int]:
    with store_db.make_session_factory(env["engine"])() as session:
        ds = session.query(Dataset).filter(Dataset.deleted_at.is_(None)).count()
        ch = session.query(Channel).filter(Channel.deleted_at.is_(None)).count()
        return ds, ch


def test_nan_batch_reports_one_chinese_error_without_traceback(env):
    """样本含 NaN：页面只出一条中文提示，不打 Traceback，库内 0 残留。"""
    folder = env["tmp_path"] / "四通道保存" / STAMP
    samples = _signal()
    samples[5] = np.nan
    for ch in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=samples,
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    at = _run(env, folder)
    _import_button(at).click()
    at.run()

    _assert_no_traceback(at)
    errors = [item.value for item in at.error]
    assert len(errors) == 1, errors
    assert "未保留" in errors[0]
    assert "非有限" in errors[0]
    assert "Traceback" not in errors[0] and "sqlite" not in errors[0]
    assert _alive_counts(env) == (0, 0)
    with store_db.make_session_factory(env["engine"])() as session:
        assert metric_store.list_values(session, env["task"].id, "CH1") == []


def test_duplicate_channel_batch_reports_chinese_error_without_traceback(env):
    """同批重复通道号：页面一条中文错误，库内 0 残留。"""
    folder = env["tmp_path"] / "四通道保存" / STAMP
    for name in (f"{STAMP}-CH1_Task1.dat", f"{STAMP}-CH1_Task2.dat"):
        write_dat(
            folder / name,
            samples=_signal(),
            channel="CH1",
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    at = _run(env, folder)
    _import_button(at).click()
    at.run()

    _assert_no_traceback(at)
    errors = [item.value for item in at.error]
    assert len(errors) == 1, errors
    assert "通道号" in errors[0] and "CH1" in errors[0]
    assert _alive_counts(env) == (0, 0)


def test_unexpected_exception_falls_back_to_chinese_message_and_logs_stack(
    env, caplog, monkeypatch
):
    """没预料到的异常（模拟写库中途炸）：中文提示 + 不暴露 SQL，堆栈进日志。"""
    folder = env["tmp_path"] / "四通道保存" / STAMP
    for ch in CHANNELS:
        write_dat(
            folder / f"{STAMP}-{ch}_Task1.dat",
            samples=_signal(),
            channel=ch,
            started_at=STAMP,
            sample_rate_hz=FS,
        )

    from lion_monitor.features import pipeline  # 页面每次运行都会重新 from-import，能被打桩

    def _boom(*_args, **_kwargs):
        raise RuntimeError(
            "database or disk is full [SQL: INSERT INTO metric_value (channel_id, code) "
            "VALUES (?, ?)] (Background on this error at: sqlalche.me/e/21/gkpj)"
        )

    monkeypatch.setattr(pipeline, "import_batch", _boom)

    caplog.set_level(logging.ERROR)
    at = _run(env, folder)
    _import_button(at).click()
    at.run()

    _assert_no_traceback(at)
    errors = [item.value for item in at.error]
    assert len(errors) == 1, errors
    text = errors[0]
    assert "导入未完成" in text
    for leak in ("INSERT", "SELECT", "no such table", "Traceback", "sqlite3", "sqlalche.me"):
        assert leak not in text, f"{leak} 泄漏到页面文案：{text}"

    stacks = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.exc_info is not None
    ]
    assert stacks, [record.getMessage() for record in caplog.records]
