"""G15：日志落盘 + 轮转。

现状（docs/reviews/2026-10-31-总体复查.md 维度 8）：全仓没有 ``logging.basicConfig`` /
文件 handler，``pipeline`` 的跳过/损坏日志只到 stderr；Streamlit 下就是那个终端窗口，
操作员看不到，现场出问题无法回溯。

口径：
- ``lion_monitor.logging_setup.setup_logging`` 统一初始化：控制台 handler 保留 +
  ``RotatingFileHandler`` 写 ``logs/lion-monitor.log``（10 MB × 5），格式含时间/级别/模块
- 启动时创建日志目录；``LION_LOG_DIR`` 可覆盖
- 重复调用不重复加 handler（Streamlit 每次 rerun 都会走入口）
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

import numpy as np
import pytest
from _dat_fixture import write_dat

from lion_monitor.features.pipeline import import_batch
from lion_monitor.logging_setup import setup_logging
from lion_monitor.store import tasks, tree
from lion_monitor.store.db import init_db, make_engine, make_session_factory

FS = 32000.0
STAMP = "20261029152834"


@pytest.fixture(autouse=True)
def _isolate_root_logger(monkeypatch):
    """每个用例后把根 logger 还原（日志配置是全局的，别串到别的用例）。"""
    monkeypatch.delenv("LION_LOG_DIR", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def test_setup_logging_writes_records_to_file(tmp_path):
    log_file = setup_logging(log_dir=tmp_path / "logs")

    assert log_file == tmp_path / "logs" / "lion-monitor.log"
    assert log_file.parent.is_dir(), "启动时应创建日志目录"

    logging.getLogger("lion_monitor.probe").warning("导入跳过 %s", "CH2")

    text = log_file.read_text(encoding="utf-8")
    assert "导入跳过 CH2" in text, text
    assert "WARNING" in text, text
    assert "lion_monitor.probe" in text, "格式里要能看出是哪个模块打的"
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text), "格式要含时间"


def test_setup_logging_defaults_under_project_root(tmp_path):
    assert setup_logging(project_root=tmp_path) == tmp_path / "logs" / "lion-monitor.log"


def test_setup_logging_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LION_LOG_DIR", str(tmp_path / "env-logs"))

    log_file = setup_logging(project_root=tmp_path)

    assert log_file == tmp_path / "env-logs" / "lion-monitor.log"
    assert log_file.parent.is_dir()


def test_setup_logging_explicit_dir_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LION_LOG_DIR", str(tmp_path / "env-logs"))

    assert setup_logging(log_dir=tmp_path / "explicit") == (
        tmp_path / "explicit" / "lion-monitor.log"
    )


def test_setup_logging_keeps_console_handler_and_is_idempotent(tmp_path):
    first = setup_logging(log_dir=tmp_path / "logs")
    second = setup_logging(log_dir=tmp_path / "logs")

    assert first == second
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1, "重复调用不能重复挂文件 handler（否则每行写两遍）"
    console = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    assert console, "控制台 handler 要保留"


def test_setup_logging_rotates_at_max_bytes(tmp_path):
    log_file = setup_logging(log_dir=tmp_path / "logs", max_bytes=1024, backup_count=2)
    logger = logging.getLogger("lion_monitor.rotate")

    for index in range(200):
        logger.warning("轮转测试 %03d %s", index, "x" * 40)

    backups = sorted(path.name for path in log_file.parent.glob("lion-monitor.log.*"))
    assert backups == ["lion-monitor.log.1", "lion-monitor.log.2"], backups
    assert log_file.stat().st_size > 0


def _batch_with_one_broken_file(tmp_path):
    """CH1 好文件 + CH2 截断文件（长度与头部声明不符 → 读失败）。"""
    folder = tmp_path / "四通道保存" / STAMP
    samples = np.sin(2 * np.pi * 100 * np.arange(int(FS * 2)) / FS).astype("float32")
    write_dat(
        folder / f"{STAMP}-CH1_Task1.dat",
        samples=samples,
        channel="CH1",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    broken = folder / f"{STAMP}-CH2_Task1.dat"
    write_dat(
        broken,
        samples=samples,
        channel="CH2",
        started_at=STAMP,
        sample_rate_hz=FS,
    )
    raw = broken.read_bytes()
    broken.write_bytes(raw[: len(raw) // 3])
    return folder


def test_import_warnings_and_summary_land_in_log_file(tmp_path):
    """验收 1：含坏文件的导入 → 日志文件里有对应的 warning 行 + 导入开始/结束。"""
    log_file = setup_logging(log_dir=tmp_path / "logs")
    engine = make_engine(tmp_path / "t.db")
    init_db(engine)
    factory = make_session_factory(engine)

    with factory() as session:
        task = tasks.create_task(session, name="日志任务")
        root = tree.root_node(session, task.id)
        device = tree.create_node(session, task.id, "device", "卷绕产线", parent_id=root.id)
        comp = tree.create_node(session, task.id, "component", "轴承A", parent_id=device.id)
        channel = tree.create_node(session, task.id, "channel", "CH1", parent_id=comp.id)
        channel2 = tree.create_node(session, task.id, "channel", "CH2", parent_id=comp.id)
        for node in (channel, channel2):
            tree.create_node(
                session, task.id, "metric", "v_RMS", parent_id=node.id, attrs={"code": "v_RMS"}
            )
        session.commit()

        folder = _batch_with_one_broken_file(tmp_path)
        result = import_batch(session, task.id, folder, {"CH1": channel.id, "CH2": channel2.id})

    assert result.files_bad_count == 1
    text = log_file.read_text(encoding="utf-8")
    assert "跳过损坏文件" in text, text
    assert f"{STAMP}-CH2_Task1.dat" in text, text
    assert "导入开始" in text, text
    assert "导入结束" in text, text
    assert result.status in text, "结束日志要带批次状态"
