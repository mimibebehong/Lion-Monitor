"""服务入口 run.py + 日志合并/中文化。

背景（2026-11-08 现场日志）：Windows 上 ``python -m streamlit run`` 走 Proactor
事件循环，遇到 ``WinError 64`` 会把监听 socket 直接关掉，服务变成「进程活着、
端口不接客」；启动器健康检查一直失败，90 秒后报启动超时。

修法：``run.py`` 在创建事件循环之前切到 ``WindowsSelectorEventLoopPolicy``，
并把 stdout/stderr、Streamlit/uvicorn 日志统一收进 ``logs/lion-monitor.log``。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

import run
from lion_monitor import logging_setup
from lion_monitor.logging_setup import (
    _ACCEPT_FAILED_HINT,
    _CORS_HINT,
    capture_streams,
    setup_logging,
    translate_console_line,
)


@pytest.fixture(autouse=True)
def _isolate_logging_and_streams():
    root = logging.getLogger()
    ours = logging.getLogger("lion_monitor")
    before_root = list(root.handlers)
    before_ours = list(ours.handlers)
    before_level = root.level
    before_console = logging_setup._console_enabled
    before_streams = (sys.stdout, sys.stderr)
    yield
    logging_setup._console_enabled = before_console
    sys.stdout, sys.stderr = before_streams
    for logger, before in ((root, before_root), (ours, before_ours)):
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
                handler.close()
        for handler in before:
            if handler not in logger.handlers:
                logger.addHandler(handler)
    root.setLevel(before_level)


def test_prepare_event_loop_switches_to_selector_on_windows():
    original = asyncio.get_event_loop_policy()
    try:
        run._prepare_event_loop()
        if sys.platform == "win32":
            assert isinstance(
                asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy
            )
    finally:
        asyncio.set_event_loop_policy(original)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Uvicorn server started on 0.0.0.0:8501", "服务已启动，监听 0.0.0.0:8501"),
        ("You can now view your Streamlit app in your browser.", ""),
        ("  Local URL: http://localhost:8501", "本机地址：http://localhost:8501"),
        ("  Network URL: http://192.168.1.101:8501", "局域网地址：http://192.168.1.101:8501"),
        ("INFO:     connection open", "连接已建立"),
        (
            'INFO:     127.0.0.1:52593 - "WebSocket /_stcore/stream" [accepted]',
            "页面已连接：127.0.0.1:52593（/_stcore/stream）",
        ),
        (
            'INFO:     127.0.0.1:52593 - "GET /_stcore/health HTTP/1.1" 200 OK',
            "访问：127.0.0.1:52593 GET /_stcore/health → 200",
        ),
        ("", ""),
        ("业务日志保持原样", "业务日志保持原样"),
    ],
)
def test_translate_console_line(raw, expected):
    assert translate_console_line(raw) == expected


def test_cors_warning_collapses_to_one_chinese_line(tmp_path):
    log_file = setup_logging(log_dir=tmp_path)

    logging.getLogger("streamlit.config").warning(
        "\n'server.enableCORS=false' tells Streamlit not to limit cross-origin access.\n"
        "Streamlit sends the header 'Access-Control-Allow-Origin: *', except on the file\n"
        "upload routes.\n"
    )

    text = log_file.read_text(encoding="utf-8")
    assert text.count(_CORS_HINT) == 1, text
    assert "cross-origin access" not in text, "英文原文不该再刷屏"


def test_asyncio_accept_noise_is_translated_and_deduplicated(tmp_path):
    log_file = setup_logging(log_dir=tmp_path)
    asyncio_logger = logging.getLogger("asyncio")

    try:
        raise OSError(64, "指定的网络名不再可用")
    except OSError:
        asyncio_logger.error(
            "Accept failed on a socket\nsocket: <asyncio.TransportSocket fd=1>",
            exc_info=True,
        )
    asyncio_logger.error("Task exception was never retrieved\nfuture: <Task finished>")

    text = log_file.read_text(encoding="utf-8")
    assert text.count(_ACCEPT_FAILED_HINT) == 1, text
    assert "Accept failed on a socket" not in text
    assert "Task exception was never retrieved" not in text
    assert "Traceback" not in text, "可忽略的 accept 噪音不带 traceback"


def test_multiline_message_is_single_line(tmp_path):
    log_file = setup_logging(log_dir=tmp_path)
    logging.getLogger("lion_monitor.probe").warning("第一行\n第二行\r\n第三行")

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert any("第一行 | 第二行 | 第三行" in line for line in lines), lines


def test_server_mode_can_disable_console_handler(tmp_path):
    setup_logging(log_dir=tmp_path)
    setup_logging(log_dir=tmp_path, console=False)

    root = logging.getLogger()
    assert not [
        handler
        for handler in root.handlers
        if getattr(handler, logging_setup._CONSOLE_HANDLER_TAG, False)
    ]


def test_capture_streams_routes_prints_into_the_log(tmp_path):
    log_file = setup_logging(log_dir=tmp_path)

    assert capture_streams()
    print("Uvicorn server started on 0.0.0.0:8501")
    print("You can now view your Streamlit app in your browser.")
    print("  URL: http://127.0.0.1:8501")
    print()
    sys.stdout.flush()

    text = log_file.read_text(encoding="utf-8")
    assert "服务已启动，监听 0.0.0.0:8501" in text, text
    assert "访问地址：http://127.0.0.1:8501" in text, text
    assert "Uvicorn server started" not in text
    assert "You can now view" not in text


def test_all_windows_entry_points_use_run_py():
    """启动器 / start.bat / NSSM 服务都必须走 run.py，不能再退回 ``-m streamlit``。"""
    root = Path(__file__).resolve().parents[1]
    for rel in ("start.bat", "launcher/LionMonitor.cs", "installer/service_install.cmd"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "run.py" in text, f"{rel} 没走 run.py"
        assert "-m streamlit" not in text, f"{rel} 还留着旧的 -m streamlit"
