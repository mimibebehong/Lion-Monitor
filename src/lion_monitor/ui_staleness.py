"""提示「源码比服务进程新，请重启」的纯逻辑（不含 streamlit 调用，便于单测）。

Streamlit 只重跑页面脚本，不会重新导入已加载的模块。看板 worker 提交新代码后，运行中的
进程仍是旧模块，页面会抛出 `ImportError: cannot import name 'xxx'` 或
`AttributeError: 'Xxx' object has no attribute 'yyy'` 这类**假报错**——符号在源码里其实存在。
所以每次渲染页面时比一比「最新源码修改时间」和「进程启动时间」，过期就提示重启。
"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

#: 进程启动瞬间可能正在写入文件，2 秒内的差异不算「代码更新」
_STARTUP_TOLERANCE_S = 2.0


def newest_source_mtime(root: Path | str) -> float | None:
    """返回 root 下所有 ``.py`` 的最新修改时间（秒）；一个都没有则返回 None。"""
    newest: float | None = None
    for path in Path(root).rglob("*.py"):
        try:
            mtime = path.stat().st_mtime
        except OSError:  # 并发删除/无权限的文件跳过
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _windows_process_started_at(pid: int) -> float | None:
    """用 ``GetProcessTimes`` 取 Windows 进程创建时间（FILETIME → 墙钟秒）。"""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        # FILETIME 是 1601-01-01 起的 100 ns 计数；11644473600 是 1601→1970 的秒数。
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 10_000_000.0 - 11_644_473_600.0
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_started_at(pid: int) -> float | None:
    """读 ``/proc/<pid>/stat`` 推算进程启动的墙钟时间（秒）；读不到返回 None。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        ticks_per_s = float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, AttributeError):
        return None
    # comm 里可能有空格和括号，按最后一个 ')' 切；切完后第一个字段是 state（第 3 列），
    # starttime 是第 22 列，所以下标是 22 - 3 = 19。
    fields = stat[stat.rfind(")") + 1 :].split()
    try:
        start_ticks = float(fields[19])
    except (IndexError, ValueError):
        return None
    return time.time() - (uptime - start_ticks / ticks_per_s)


def process_started_at(pid: int | None = None) -> float | None:
    """进程启动的墙钟时间（秒）；Windows 走 Win32 API，其他平台读 ``/proc``。"""
    target = os.getpid() if pid is None else pid
    if os.name == "nt":
        try:
            return _windows_process_started_at(target)
        except (OSError, ValueError):
            return None
    return _posix_process_started_at(target)


def stale_notice(
    *,
    started_at: float | None,
    newest_mtime: float | None,
    tolerance_s: float = _STARTUP_TOLERANCE_S,
) -> str | None:
    """源码比进程新 → 返回一行提示文案；否则（或信息不全时）返回 None。"""
    if started_at is None or newest_mtime is None:
        return None
    if newest_mtime <= started_at + tolerance_s:
        return None
    # 带秒：进程启动和文件改动常常在同一分钟，只到分钟会看不出先后
    changed = time.strftime("%H:%M:%S", time.localtime(newest_mtime))
    started = time.strftime("%H:%M:%S", time.localtime(started_at))
    return (
        f"代码已更新（{changed}），服务进程是 {started} 启动的 —— 报错请先重启服务"
        "（Streamlit 不会重新导入已加载的模块）"
    )
