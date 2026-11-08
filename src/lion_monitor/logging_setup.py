"""统一日志初始化：控制台 + 轮转文件。

复查 G15：全仓没有 ``logging.basicConfig`` / 文件 handler，导入跳过、坏文件这些日志只
落在 ``stderr``——Streamlit 下就是那个终端窗口，操作员看不到，现场出问题无法回溯。

口径：

- 文件：``<工程根>/logs/lion-monitor.log``，``RotatingFileHandler`` 10 MB × 5
- 控制台 handler 保留（开发/排障仍旧看终端）；服务模式（``run.py``）下关闭并把
  stdout/stderr 一起收进同一个文件，现场只需要看一个日志
- 格式含时间 / 级别 / 模块名；多行消息压成一行，避免一条 16 行的英文警告刷屏
- 第三方（Streamlit / uvicorn / asyncio）能翻成中文的尽量翻，纯噪音直接丢掉
- 日志目录可用环境变量 ``LION_LOG_DIR`` 覆盖；启动时自动创建
- 重复调用不重复挂 handler（Streamlit 每次 rerun 都会走入口）
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = [
    "BACKUP_COUNT",
    "LOG_FILE_NAME",
    "LOG_FORMAT",
    "MAX_BYTES",
    "capture_streams",
    "resolve_log_dir",
    "set_console_enabled",
    "setup_logging",
    "take_over_streamlit_loggers_permanently",
    "translate_console_line",
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

#: 服务模式（run.py）是否保留控制台 handler；关闭后 stdout/stderr 由 capture_streams 收编
_console_enabled = True

#: 第三方日志里能翻成中文的固定说法（英文前缀 → 中文前缀；空串表示丢弃该行）
_MESSAGE_TRANSLATIONS = (
    ("Uvicorn server started on ", "服务已启动，监听 "),
    ("You can now view your Streamlit app in your browser.", ""),
    ("Local URL:", "本机地址："),
    ("Network URL:", "局域网地址："),
    ("External URL:", "外网地址："),
    ("URL:", "访问地址："),
    ("INFO:     Waiting for application startup.", "正在启动应用…"),
    ("INFO:     Application startup complete.", "应用启动完成"),
    ("INFO:     Started server process", "服务进程已启动"),
    ("INFO:     Uvicorn running on", "服务监听"),
    ("INFO:     Shutting down", "正在关闭服务…"),
    ("INFO:     Finished server process", "服务进程已结束"),
    ("INFO:     connection open", "连接已建立"),
    ("INFO:     connection closed", "连接已断开"),
    ("INFO:     connection lost", "连接已断开"),
    ("WARNING:  ", "警告："),
    ("ERROR:    ", "错误："),
)

#: uvicorn 的访问行；形如 ``127.0.0.1:1234 - "WebSocket /_stcore/stream" [accepted]``
_UVICORN_WS_RE = re.compile(
    r'^INFO:\s+(?P<addr>\S+) - "WebSocket (?P<path>[^"]+)" \[accepted\]$'
)
_UVICORN_ACCESS_RE = re.compile(
    r'^INFO:\s+(?P<addr>\S+) - "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d+)'
)

#: Streamlit 默认日志格式的时间戳前缀（如 ``2026-11-08 11:08:56.112 ``）
_STREAMLIT_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s*")

#: Streamlit 1.66 在 enableCORS=false 时打 16 行英文安全提示；我们只要一行中文
_CORS_HINT = (
    "跨域保护未开启（server.enableCORS=false）：任何网页都能连本服务，"
    "仅建议在受信局域网内使用"
)

#: asyncio 在 Windows 上遇到“客户端连上就断开”时打的控制台告警；不是业务错误
_ACCEPT_FAILED_HINT = (
    "接收连接失败：客户端在服务接受连接前断开，已忽略（不影响服务运行）"
)


def translate_console_line(line: str) -> str:
    """把 Streamlit/uvicorn 的启动横幅翻成中文；空行返回空串（调用方直接丢弃）。"""
    text = (line or "").replace("\x00", "").strip()
    # Streamlit 自己的日志格式会带毫秒时间戳，转进我们的日志后由我们统一加时间
    text = _STREAMLIT_TIMESTAMP_RE.sub("", text)
    text = text.replace(" (Press CTRL+C to quit)", "")
    if not text:
        return ""

    match = _UVICORN_WS_RE.match(text)
    if match:
        return f"页面已连接：{match.group('addr')}（{match.group('path')}）"
    match = _UVICORN_ACCESS_RE.match(text)
    if match:
        return (
            f"访问：{match.group('addr')} {match.group('method')} "
            f"{match.group('path')} → {match.group('status')}"
        )

    for english, chinese in _MESSAGE_TRANSLATIONS:
        if text == english:
            return chinese
        if text.startswith(english):
            return (chinese + text[len(english):].strip()).strip()
    return text


class _DiagnosticFilter(logging.Filter):
    """洗掉第三方噪音：能翻的翻成中文，纯重复的直接丢。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式串坏了也别把日志本身弄崩
            return True

        if record.name == "asyncio" or record.name.startswith("asyncio."):
            if "Task exception was never retrieved" in message:
                return False
            if "Accept failed on a socket" in message:
                record.msg = _ACCEPT_FAILED_HINT
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                return True

        if "tells Streamlit not to limit cross-origin access" in message:
            record.msg = _CORS_HINT
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            return True

        if message.startswith("Uvicorn server started on "):
            record.msg = "服务已启动，监听 " + message[len("Uvicorn server started on "):]
            record.args = ()
            return True

        return True


class _DiagnosticFormatter(logging.Formatter):
    """多行消息压成一行；traceback 单独保留（真出错时还是要能回溯）。"""

    def format(self, record: logging.LogRecord) -> str:
        if not record.exc_info:
            message = record.getMessage()
            if "\n" in message or "\r" in message:
                parts = [
                    part.strip()
                    for part in message.replace("\r", "\n").split("\n")
                    if part.strip()
                ]
                record.msg = " | ".join(parts)
                record.args = ()
        return super().format(record)


class _LogStream(io.TextIOBase):
    """把 print()/第三方库写 stdout/stderr 的内容按行转进 logging。"""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""
        self._lock = threading.Lock()
        self._lion_log_stream = True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:  # type: ignore[override]
        if not text:
            return 0
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", "replace")
        elif not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._buffer += text
            self._drain(final=False)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            # 只冲刷完整行：Streamlit 会先写 "  URL: " 再写地址，中间夹一次 flush，
            # 这里不能把半行拆成两条日志。
            self._drain(final=False)

    def _drain(self, *, final: bool) -> None:
        while True:
            indexes = [
                index
                for index in (self._buffer.find("\n"), self._buffer.find("\r"))
                if index >= 0
            ]
            if indexes:
                index = min(indexes)
                line = self._buffer[:index]
                self._buffer = self._buffer[index + 1:]
            elif final:
                index = -1
                line = self._buffer
                self._buffer = ""
            else:
                return

            level = self._level
            raw = line.strip()
            if raw.startswith("INFO:"):
                level = logging.INFO
            elif raw.startswith(("ERROR", "CRITICAL")):
                level = logging.ERROR

            text = translate_console_line(line)
            if text:
                self._logger.log(level, text)
            if index < 0:
                return


def set_console_enabled(enabled: bool) -> None:
    """服务模式关掉控制台 handler，避免同一条日志同时写进两个文件。"""
    global _console_enabled
    _console_enabled = bool(enabled)
    if _console_enabled:
        return
    for logger in (logging.getLogger(), logging.getLogger(_OWN_LOGGER_PREFIX)):
        for handler in list(logger.handlers):
            if getattr(handler, _CONSOLE_HANDLER_TAG, False):
                logger.removeHandler(handler)
                handler.close()


def capture_streams(level: int = logging.INFO) -> bool:
    """把 stdout/stderr 收进日志（服务模式）；已经是我们的流则不重复接管。"""
    if getattr(sys.stdout, "_lion_log_stream", False):
        return False
    out = _LogStream(logging.getLogger("stdout"), level)
    err = _LogStream(logging.getLogger("stderr"), logging.WARNING)
    sys.stdout = out
    sys.stderr = err
    atexit.register(out.flush)
    atexit.register(err.flush)
    return True


def take_over_streamlit_loggers_permanently() -> bool:
    """让 Streamlit 之后**懒创建**的 logger 也冒泡到我们的 handler。

    ``streamlit.logger.get_logger()`` 在第一次调用时才给 logger 挂 stderr handler 并
    ``propagate=False``；启动时遍历 ``loggerDict`` 抓不到（config、uvicorn 都是运行时
    才创建）。而且 Streamlit 解析完配置还会调 ``update_formatter()`` 把 handler 再挂
    回来。这里把这两个入口包一层：
    - ``get_logger``：创建后摘掉 console handler、强制 propagate=True；
    - ``setup_formatter``：不再往 stderr 挂 handler（我们已经统一收进文件）。
    """
    try:
        import streamlit.logger as streamlit_logger
    except Exception:  # noqa: BLE001 - 没有 streamlit（纯算法测试）时直接跳过
        return False

    if getattr(streamlit_logger, "_lion_taken_over", False):
        return True

    original_get_logger = streamlit_logger.get_logger

    def get_logger(name: str) -> logging.Logger:
        logger = original_get_logger(name)
        logger.propagate = True
        handler = getattr(logger, "streamlit_console_handler", None)
        if handler is not None and handler in logger.handlers:
            logger.removeHandler(handler)
        return logger

    def setup_formatter(logger: logging.Logger) -> None:
        logger.propagate = True

    streamlit_logger.get_logger = get_logger
    streamlit_logger.setup_formatter = setup_formatter
    streamlit_logger._lion_taken_over = True
    return True


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
    return _DiagnosticFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def _noise_filter() -> logging.Filter:
    return _DiagnosticFilter()


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
    console: bool | None = None,
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
        handler.addFilter(_noise_filter())
        setattr(handler, _FILE_HANDLER_TAG, True)
        root.addHandler(handler)

    # 控制台 handler：交互运行（start.bat）保留；服务模式（run.py）关闭并改为
    # 把 stdout/stderr 收进同一个文件，避免两份日志各写一半。
    use_console = _console_enabled if console is None else bool(console)
    if use_console:
        if not _marked(_CONSOLE_HANDLER_TAG):
            console_handler = logging.StreamHandler()
            console_handler.setLevel(level)
            console_handler.setFormatter(_formatter())
            console_handler.addFilter(_noise_filter())
            setattr(console_handler, _CONSOLE_HANDLER_TAG, True)
            root.addHandler(console_handler)
    else:
        for logger in (root, logging.getLogger(_OWN_LOGGER_PREFIX)):
            for console_handler in list(logger.handlers):
                if getattr(console_handler, _CONSOLE_HANDLER_TAG, False):
                    logger.removeHandler(console_handler)
                    console_handler.close()

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
    if not file_handlers:
        return

    ours = logging.getLogger(_OWN_LOGGER_PREFIX)
    ours.disabled = False
    for handler in (*file_handlers, *console_handlers):
        if handler not in ours.handlers:
            ours.addHandler(handler)
    # 服务模式没有控制台 handler：之前可能挂过，顺手摘干净
    for handler in list(ours.handlers):
        if (
            getattr(handler, _CONSOLE_HANDLER_TAG, False)
            and handler not in console_handlers
        ):
            ours.removeHandler(handler)
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
