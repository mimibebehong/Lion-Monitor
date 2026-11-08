"""P1 页面回归：批次事务失败只报一条中文错误；批次三态在页面上显示「部分有效」。

口径（本卡定，马哥 Q3 拍板）：

- 计算中途抛异常（节点参数 ``band_low=0`` 让 ``butter`` 报错）→ 页面只出**一条中文**
  提示、不打 Traceback，**库内 0 行新增**（整批回滚）
- 4 个文件里 1 个损坏 → 导入摘要与数据集列表都显示「部分有效」，并点名文件与原因
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from _dat_fixture import write_dat

ROOT = Path(__file__).resolve().parents[1]
INGEST_PAGE = ROOT / "app" / "pages" / "ingest.py"
DATASETS_PAGE = ROOT / "app" / "pages" / "datasets.py"
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.store import datasets, metric_assign, tasks, tree  # noqa: E402
from lion_monitor.store import db as store_db  # noqa: E402
from lion_monitor.store import metrics as metric_store  # noqa: E402
from lion_monitor.store.models import Channel, Dataset  # noqa: E402

FS = 32000.0
STAMP = "20261029152834"
CHANNELS = ("CH1", "CH2", "CH3", "CH4")


def _signal() -> np.ndarray:
    t = np.arange(int(FS * 2)) / FS
    return np.sin(2 * np.pi * 100 * t).astype("float32")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / "p1-page.db"
    monkeypatch.setenv("LION_DB", str(path))
    engine = store_db.make_engine(path)
    store_db.init_db(engine)
    factory = store_db.make_session_factory(engine)

    with store_db.session_scope(factory) as session:
        task = tasks.create_task(session, name="任务批次三态")
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
        "点数": int(FS * 2),
        "采样率": FS,
        "时长(s)": 2.0,
        "文件数": 4,
    }


def _run(page: Path, env, folder: Path | None = None):
    import store_ctx  # noqa: E402
    from streamlit.testing.v1 import AppTest  # noqa: E402

    store_ctx._session_factory.clear()
    at = AppTest.from_file(str(page), default_timeout=180)
    at.session_state["current_task_id"] = env["task"].id
    if folder is not None:
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


def _batch_dir(tmp_path, stamp: str = STAMP) -> Path:
    folder = tmp_path / "四通道保存" / stamp
    for ch in CHANNELS:
        write_dat(
            folder / f"{stamp}-{ch}_Task1.dat",
            samples=_signal(),
            channel=ch,
            started_at=stamp,
            sample_rate_hz=FS,
        )
    return folder


def _bad_band_assignment(env, channel_code: str) -> None:
    """给该通道的 v_RMS 授非数字 ``band_low``：计算必抛 ValueError（复现复查 §4.1）。

    Q9 之后 ``band_low=0`` 被硬下限兜住、不再抛异常，所以改用脏 JSON 值复现。
    """
    factory = store_db.make_session_factory(env["engine"])
    with store_db.session_scope(factory) as session:
        node = next(
            item
            for item in tree.list_nodes(session, env["task"].id)
            if item.node_type == "channel" and item.name == channel_code
        )
        metric_assign.assign_metric(
            session, env["task"].id, node.id, "v_RMS", params={"band_low": "abc"}
        )


def test_compute_failure_reports_one_chinese_error_and_leaves_no_rows(env):
    """计算中途抛异常：页面一条中文提示、无 Traceback、库内 0 行新增。"""
    folder = _batch_dir(env["tmp_path"])
    _bad_band_assignment(env, "CH4")

    at = _run(INGEST_PAGE, env, folder)
    _import_button(at).click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    errors = [item.value for item in at.error]
    assert len(errors) == 1, errors
    text = errors[0]
    assert "导入未完成" in text and "没有正常写入" in text
    for leak in ("butter", "Traceback", "sqlite", "filter critical"):
        assert leak not in text, f"{leak} 泄漏到页面文案：{text}"

    # 整批回滚：0 数据集 / 0 通道 / 0 指标值
    assert _alive_counts(env) == (0, 0)
    with store_db.make_session_factory(env["engine"])() as session:
        assert datasets.list_datasets(session, env["task"].id) == []
        assert metric_store.list_values(session, env["task"].id, "CH1") == []


def test_one_damaged_file_shows_partial_in_summary_and_dataset_list(env):
    """4 个文件里 1 个损坏：导入摘要写「部分有效」并点名文件/原因；数据集列表同样显示。"""
    folder = _batch_dir(env["tmp_path"])
    bad_name = f"{STAMP}-CH2_Task1.dat"
    raw = (folder / bad_name).read_bytes()
    (folder / bad_name).write_bytes(raw[: len(raw) // 3])

    at = _run(INGEST_PAGE, env, folder)
    _import_button(at).click()
    at.run()

    assert [exc.value for exc in at.exception] == [], [e.value for e in at.exception]
    success = [item.value for item in at.success]
    assert len(success) == 1, success
    summary = success[0]
    assert "部分有效" in summary
    assert bad_name in summary
    assert "长度不一致" in summary

    with store_db.make_session_factory(env["engine"])() as session:
        rows = datasets.list_datasets(session, env["task"].id)
        assert len(rows) == 1
        assert rows[0].status == "部分有效"
        assert bad_name in rows[0].remark
        # 值只来自 3 个成功的通道
        for code in ("CH1", "CH3", "CH4"):
            assert len(metric_store.list_values(session, env["task"].id, code)) == 2
        assert metric_store.list_values(session, env["task"].id, "CH2") == []

    # 数据集管理页：列表状态列 = 部分有效，KPI 说明带「部分有效 1 个」
    page = _run(DATASETS_PAGE, env)
    assert [exc.value for exc in page.exception] == [], [e.value for e in page.exception]
    frame = page.dataframe[0].value
    assert list(frame["状态"]) == ["部分有效"]
    captions = [item.value for item in page.caption]
    assert any("部分有效 1 个" in text for text in captions), captions
