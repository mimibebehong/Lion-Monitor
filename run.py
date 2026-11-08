"""Lion-Monitor 服务入口：绿色包 / 安装包实际运行的就是这个文件。

为什么不再用 ``python -m streamlit run``：

Windows 上 asyncio 默认使用 Proactor 事件循环。CPython 3.11 的 Proactor 在遇到
``WinError 64``（客户端连上后立刻断开）时会**关掉整个监听 socket 且不再 accept**，
服务表现成「进程活着、端口不再接客」——0.1.1 现场日志里已复现：启动探活正好踩中，
90 秒后启动器报「启动超时」。Selector 事件循环用 select()+accept()，不会因为单个
连接被重置就废掉监听。

顺带把启动横幅、Streamlit 日志和 print() 输出收进 ``logs/lion-monitor.log``：
服务模式下现场只需要看这一个日志文件。

启动方式（和 ``streamlit run`` 参数完全一致）::

    python run.py run app\\main.py --server.port 8501
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _prepare_event_loop() -> None:
    """必须在任何事件循环创建之前调用（Streamlit 用 ``asyncio.run()`` 起服务）。"""
    if sys.platform != "win32":
        return
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _should_capture() -> bool:
    """服务模式（启动器 / NSSM）把 stdout/stderr 收进日志；控制台模式保留输出。"""
    override = (os.environ.get("LION_LOG_CAPTURE") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    if override in {"1", "true", "yes", "on"}:
        return True
    isatty = getattr(sys.stdout, "isatty", None)
    return not (callable(isatty) and isatty())


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def main(argv: list[str] | None = None) -> int:
    _prepare_event_loop()

    from lion_monitor.logging_setup import (
        capture_streams,
        set_console_enabled,
        setup_logging,
        take_over_streamlit_loggers_permanently,
    )

    capture = _should_capture()
    set_console_enabled(not capture)
    setup_logging(_ROOT)
    if capture:
        capture_streams()

    logger = logging.getLogger("lion_monitor.server")
    logger.info(
        "启动 Lion-Monitor 服务（事件循环：%s）",
        "Selector（Windows 防 accept 断链）" if sys.platform == "win32" else "默认",
    )

    try:
        # 先 import 再 setup 一次：把 streamlit.* 各模块自带的 stderr handler 全部接管，
        # 启动期的 CORS 提示、uvicorn 启动行才会写进 lion-monitor.log。
        import streamlit.web.cli as streamlit_cli

        take_over_streamlit_loggers_permanently()
        setup_logging(_ROOT)

        if argv is not None:
            sys.argv = ["streamlit", *argv]
        streamlit_cli.main()
    except SystemExit as exc:
        code = _exit_code(exc)
        logger.info("服务进程退出（退出码 %s）", code)
        return code
    except KeyboardInterrupt:
        logger.info("服务收到中断，已退出")
        return 0
    except BaseException:  # noqa: BLE001 - 启动失败必须留下 traceback
        logger.exception("服务启动失败")
        return 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass

    logger.info("服务已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
