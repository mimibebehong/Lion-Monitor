"""网页端目录浏览器（马哥 2026-11-04 同意：局域网多人访问 + 服务自启，原生弹窗看不到）。

约定（页面接入方按这个用）：

- ``drives()``       → [(显示名, 路径)]，Windows 是盘符，Linux 是 "/"
- ``list_subdirs()`` → 只列**子目录**（不列文件），按名字不区分大小写排序
- ``parent_of()``    → 上一级；到根返回 None
- ``validate_dir()``  → None 表示可用，否则返回一句人话原因（页面直接显示）

任何失败都抛 :class:`DirError`，消息是**给现场用户看的**中文说明，
页面捕获后原样显示 —— 不允许静默返回空列表（马哥要求「不要没反应」）。
"""

from __future__ import annotations

import os
import string
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DirEntry",
    "DirError",
    "drives",
    "is_windows",
    "list_subdirs",
    "parent_of",
    "validate_dir",
]

#: 列目录的默认超时（秒）。网络盘掉线时 os.scandir 可能长时间阻塞，页面不能干等。
DEFAULT_TIMEOUT_S = 5.0


class DirError(Exception):
    """目录操作失败，message 是可以直接显示给用户的中文原因。"""


@dataclass(frozen=True)
class DirEntry:
    """一个子目录。``name`` 是显示名，``path`` 是完整路径。"""

    name: str
    path: str


def is_windows() -> bool:
    return sys.platform == "win32"


def _normalize(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).absolute()


def drives() -> list[tuple[str, str]]:
    """可用磁盘根列表：Windows 是 ``[('C:\\\\', 'C:\\\\'), ...]``，其他平台是 ``[('/', '/')]``。"""
    if not is_windows():
        return [("/", "/")]
    found: list[tuple[str, str]] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        try:
            if os.path.exists(root):
                found.append((root, root))
        except OSError:  # 无盘符设备（如断开的网络盘）直接跳过
            continue
    return found


def _is_hidden(path: Path, name: str) -> bool:
    """隐藏判定：点前缀（跨平台）+ Windows 的隐藏属性。"""
    if name.startswith("."):
        return True
    if not is_windows():
        return False
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & 0x2)  # FILE_ATTRIBUTE_HIDDEN


def _scan(path: Path, show_hidden: bool) -> list[DirEntry]:
    entries: list[DirEntry] = []
    with os.scandir(path) as it:
        for item in it:
            try:
                if not item.is_dir(follow_symlinks=False):
                    continue
            except OSError:  # 单个条目读不到（权限/断链）跳过，不打断整次列举
                continue
            if not show_hidden and _is_hidden(Path(item.path), item.name):
                continue
            entries.append(DirEntry(name=item.name, path=item.path))
    entries.sort(key=lambda e: e.name.lower())
    return entries


def list_subdirs(
    path: str | Path,
    *,
    show_hidden: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[DirEntry]:
    """列出 ``path`` 下的子目录。

    :raises DirError: 目录不存在 / 不是目录 / 没权限 / 读取超时（消息可直接显示）
    """
    target = _normalize(path)
    if not target.exists():
        raise DirError(f"目录「{target}」不存在或已被移除，已回到上一级")
    if not target.is_dir():
        raise DirError(f"「{target}」不是文件夹，请换一个目录")

    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = _scan(target, show_hidden)
        except PermissionError:
            box["error"] = DirError(
                f"当前运行账号无权访问「{target}」。"
                "如果是网络映射盘，请改用 \\\\server\\share 形式的路径"
            )
        except OSError as exc:
            box["error"] = DirError(f"读取「{target}」失败：{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=run, name="dir-browser-scan", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise DirError(f"读取「{target}」超时（{timeout_s:.0f} 秒），可能是网络盘无响应或磁盘太慢")
    if isinstance(box.get("error"), DirError):
        raise box["error"]  # type: ignore[misc]
    return box.get("value") or []  # type: ignore[return-value]


def parent_of(path: str | Path) -> str | None:
    """上一级目录；已经是根（``C:\\`` 或 ``/``）时返回 None。"""
    target = _normalize(path)
    if target.parent == target:
        return None
    return str(target.parent)


def validate_dir(path: str | Path) -> str | None:
    """校验目录可用：返回 None 表示 OK，否则返回一句中文原因。"""
    if not str(path).strip():
        return "请先选择或填写数据目录"
    target = _normalize(path)
    if not target.exists():
        return f"目录「{target}」不存在"
    if not target.is_dir():
        return f"「{target}」不是文件夹"
    try:
        next(os.scandir(target), None)
    except PermissionError:
        return f"当前运行账号无权访问「{target}」"
    except OSError as exc:
        return f"读取「{target}」失败：{type(exc).__name__}: {exc}"
    return None
