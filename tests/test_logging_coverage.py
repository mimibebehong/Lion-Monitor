"""日志覆盖：2026-11-05「报错信息都会报错到 log 吧，方便日后运营」。

三层保证：

1. 页面未捕获异常 → ``main.py`` 里包住 ``pg.run()`` 记 traceback 后再抛（页面照常显示报错）
2. Streamlit 内部 logger（每个模块各自 addHandler + propagate=False）→ ``setup_logging`` 全量接管
3. 业务错误提示 → ``ui.flash(tone="error"/"warning")`` 与 ``ui.error()/ui.warning()`` 统一落盘
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from lion_monitor.logging_setup import LOG_FILE_NAME, setup_logging  # noqa: E402


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LION_LOG_DIR", str(tmp_path))
    yield tmp_path


def _log_text(tmp_path: Path) -> str:
    path = tmp_path / LOG_FILE_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_uncaught_page_exception_lands_in_log(log_dir, tmp_path):
    """页面里没人接的异常必须留下 traceback —— 服务模式没有终端，丢了就查无此案。"""
    import streamlit  # noqa: F401  真实 main.py 就是这个顺序：先 import streamlit
    from streamlit.testing.v1 import AppTest

    setup_logging(ROOT)
    boom = tmp_path / "boom_page.py"
    boom.write_text(
        "import streamlit as st\nraise RuntimeError('故意触发：日志覆盖测试')\n",
        encoding="utf-8",
    )

    at = AppTest.from_file(str(boom), default_timeout=30)
    at.run()

    assert at.exception, "页面应当显示异常"
    text = _log_text(log_dir)
    assert "Uncaught app execution" in text or "故意触发：日志覆盖测试" in text, text[-800:]


def test_flash_error_and_warning_are_logged(log_dir, caplog):
    import ui

    setup_logging(ROOT)
    with caplog.at_level(logging.INFO, logger="lion_monitor.ui"):
        ui.flash("扫描失败：目录不可读", icon="⚠️", tone="error")
        ui.flash("整批没有有效文件", icon="⚠️", tone="warning")
        ui.flash("导入成功 3 个批次", tone="success")

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "扫描失败：目录不可读" in joined
    assert "整批没有有效文件" in joined
    assert "导入成功 3 个批次" not in joined, "成功提示不该写日志（会淹没真错误）"


def test_ui_error_and_warning_are_logged(log_dir, caplog):
    import ui

    setup_logging(ROOT)
    with caplog.at_level(logging.INFO, logger="lion_monitor.ui"):
        ui.error("建库失败：磁盘只读")
        ui.warning("没有扫描到批次")

    records = [r for r in caplog.records if r.name == "lion_monitor.ui"]
    assert any("建库失败：磁盘只读" in r.getMessage() for r in records)
    assert any(r.levelno == logging.ERROR for r in records), "error() 应当记 ERROR 级"
    assert any(r.levelno == logging.WARNING for r in records)


def test_streamlit_module_loggers_are_taken_over(log_dir):
    """Streamlit 给每个模块单装 handler 且 propagate=False，必须被改成冒泡。"""
    import streamlit  # noqa: F401
    import streamlit.runtime.scriptrunner.script_runner  # noqa: F401

    setup_logging(ROOT)
    external = logging.getLogger("streamlit.runtime.scriptrunner.script_runner")
    assert external.propagate is True
    assert not external.handlers


def test_business_logger_uses_same_file(log_dir):
    """业务层（导入/重算）自己的 logger 也要落到同一个文件。"""
    setup_logging(ROOT)
    logger = logging.getLogger("lion_monitor.features.pipeline")
    assert logger.getEffectiveLevel() <= logging.INFO
    logger.info("业务日志落盘验证")
    assert "业务日志落盘验证" in _log_text(log_dir)


def test_ui_module_initializes_logging_for_any_entry_point():
    """深链接打开 /ingest 时 Streamlit 不执行 app/main.py，只能靠 ui.py 初始化日志。

    2026-11-05 实测：直接访问页面 URL 时 setup_logging 被跳过，trace 文件为空、
    logs/lion-monitor.log 完全不写。把初始化放进 ui.py（每个页面都 import）才覆盖所有入口。
    """
    ui_source = Path(__file__).resolve().parents[1] / "app" / "ui.py"
    text = ui_source.read_text(encoding="utf-8")

    assert "setup_logging(" in text, "ui.py 必须初始化日志，否则深链接入口没有日志"
    assert "store_ctx" in text, "ui.py 需要用 store_ctx.PROJECT_ROOT 定位日志目录"
