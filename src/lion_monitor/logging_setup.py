"""统一日志初始化：控制台 + 轮转文件。

复查 G15：全仓没有 ``logging.basicConfig`` / 文件 handler，导入跳过、坏文件这些日志只
落在 ``stderr``——Streamlit 下就是那个终端窗口，操作员看不到，现场出问题无法回溯。

口径：

- 文件：``<工程根>/logs/lion-monitor.log``，``RotatingFileHandler`` 10 MB × 5
- 控制台 handler 保留（开发/排障仍旧看终端）
- 格式含时间 / 级别 / 模块名
- 日志目录可用环境变量 ``LION_LOG_DIR`` 覆盖；启动时自动创建
- 重复调用不重复挂 handler（Streamlit 每次 rerun 都会走入口）
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "BACKUP_COUNT",
    "LOG_FILE_NAME",
    "LOG_FORMAT",
    "MAX_BYTES",
    "resolve_log_dir",
    "setup_logging",
]

#: 日志文件名（轮转后的备份是同目录下的 .1 / .2 …）
LOG_FILE_NAME = "lion-monitor.log"

#: 单个日志文件上限 10 MB，保留 5 份备份
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

#: 时间 / 级别 / 模块名 / 消息
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Streamlit 给**每个模块级 logger** 都单独装了 stderr handler 且 propagate=False
#: （`streamlit.logger` / `streamlit.runtime.scriptrunner.script_runner` …），
#: 所以未捕获的页面异常只打在终端上 —— 服务模式没有终端，现场等于没记录。
#: 这里把它们全部接管，让日志冒泡到我们的文件 handler。
_STREAMLIT_PREFIX = "streamlit"

#: 我们自己挂的 handler 打标记，重复初始化时据此复用/替换（别重复写文件）
_FILE_HANDLER_TAG = "_lion_logging_file"
_CONSOLE_HANDLER_TAG = "_lion_logging_console"


def resolve_log_dir(project_root: str | Path | None = None, override: str | None = None) -> Path:
    """日志目录：显式目录 > 环境变量 ``LION_LOG_DIR`` > ``<工程根>/logs``。"""
    text = (override or "").strip()
    if text:
        return Path(text).expanduser()
    env = (os.environ.get("LION_LOG_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    root = Path(project_root) if project_root is not None else Path.cwd()
    return root / "logs"


def _formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def _marked(attribute: str) -> list[logging.Handler]:
    root = logging.getLogger()
    return [handler for handler in root.handlers if getattr(handler, attribute, False)]


def setup_logging(
    project_root: str | Path | None = None,
    *,
    log_dir: str | Path | None = None,
    level: int = logging.INFO,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> Path:
    """初始化根 logger，返回日志文件路径（可重复调用）。"""
    directory = Path(log_dir) if log_dir is not None else resolve_log_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    # 文件 handler：同一目标复用，换了目录就换掉（避免两个文件各写一半）
    existing = [
        handler
        for handler in _marked(_FILE_HANDLER_TAG)
        if isinstance(handler, RotatingFileHandler)
    ]
    same_target = existing and (
        Path(existing[0].baseFilename) == log_file
        and existing[0].maxBytes == max_bytes
        and existing[0].backupCount == backup_count
    )
    if not same_target:
        for handler in existing:
            root.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(_formatter())
        setattr(handler, _FILE_HANDLER_TAG, True)
        root.addHandler(handler)

    # 控制台 handler：保留 terminal 上的可见性（Streamlit 下就是启动那个窗口）
    if not _marked(_CONSOLE_HANDLER_TAG):
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(_formatter())
        setattr(console, _CONSOLE_HANDLER_TAG, True)
        root.addHandler(console)

    # 马哥 2026-11-05：页面里未捕获的异常必须落盘（现场要能回溯）。
    _take_over_streamlit_loggers(level)
    _attach_own_handlers(level)
    return log_file


#: 我们自己的 logger 家族。直接挂 handler，不依赖 root —— Streamlit 1.66（uvicorn/starlette
#: 架构）在某些时机重置 root logging，只挂 root 会出现「页面有提示、日志文件不更新」。
_OWN_LOGGER_PREFIX = "lion_monitor"


def _attach_own_handlers(level: int) -> None:
    """把文件 + 控制台 handler 直接挂到 ``lion_monitor.*`` 上，并关掉向 root 冒泡（避免重复）。"""
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if getattr(h, _FILE_HANDLER_TAG, False)]
    console_handlers = [h for h in root.handlers if getattr(h, _CONSOLE_HANDLER_TAG, False)]
    if not file_handlers or not console_handlers:
        return

    ours = logging.getLogger(_OWN_LOGGER_PREFIX)
    ours.disabled = False
    for handler in (*file_handlers, *console_handlers):
        if handler not in ours.handlers:
            ours.addHandler(handler)
    ours.propagate = False
    if ours.level == logging.NOTSET or ours.level > level:
        ours.setLevel(level)


def _take_over_streamlit_loggers(level: int) -> int:
    """把所有 ``streamlit*`` logger 改成冒泡到 root，返回接管的个数。

    Streamlit 在 ``streamlit/logger.py`` 里给每个模块建 logger 时都 addHandler(stderr)
    并 ``propagate = False``，只处理父 logger 拦不住子 logger 的输出。
    ``setup_logging`` 每次 rerun 都会执行，所以运行中新出现的 logger 也会被后续接管。
    """
    candidates = [logging.getLogger(_STREAMLIT_PREFIX)]
    for name, obj in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith(_STREAMLIT_PREFIX + ".") and isinstance(obj, logging.Logger):
            candidates.append(obj)

    taken = 0
    for external in candidates:
        if not external.handlers and external.propagate:
            continue
        for handler in list(external.handlers):
            external.removeHandler(handler)
        external.propagate = True
        if external.level == logging.NOTSET or external.level > level:
            external.setLevel(level)
        taken += 1
    return taken
