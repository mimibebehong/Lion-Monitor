"""界面级持久化设置（2026-11-04：数据目录自己选，但要记住上次选择的）。

为什么不用 ``st.session_state``：那个只活在**当前浏览器会话**里，关掉页面或重启服务就丢，
现场每次开机都要重新选一遍目录。

为什么不用数据库表：这些是「这台主机怎么用」的设置，不是业务数据；
放一个 JSON 文件里既好读、又能随 ``data\\`` 一起备份迁移，还不用改表结构。

位置：``<工程根>/data/ui_settings.json``，可用 ``LION_UI_SETTINGS`` 覆盖整个文件路径。
读写失败一律**不抛异常**（页面不能因为一个配置文件损坏就打不开），最坏退化成「没记住」。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

__all__ = [
    "SETTINGS_ENV",
    "get_scan_dir",
    "load_settings",
    "remember_scan_dir",
    "settings_path",
]

#: 覆盖设置文件路径的环境变量（测试 / 特殊部署用）
SETTINGS_ENV = "LION_UI_SETTINGS"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_KEY_SCAN_DIR = "scan_dir"


def settings_path() -> Path:
    """设置文件路径：环境变量优先，否则 ``<工程根>/data/ui_settings.json``。"""
    raw = (os.environ.get(SETTINGS_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _PROJECT_ROOT / "data" / "ui_settings.json"


def load_settings() -> dict[str, object]:
    """读设置；文件不存在、不是合法 JSON、不是对象 → 返回空 dict。"""
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_scan_dir() -> str | None:
    """上次记住的数据目录；没记住或值不合法 → None。"""
    value = load_settings().get(_KEY_SCAN_DIR)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def remember_scan_dir(path: str | None) -> None:
    """记住数据目录（空值视为「清除记忆」）。写失败静默忽略，不影响页面。"""
    data = load_settings()
    text = (path or "").strip()
    if text:
        data[_KEY_SCAN_DIR] = text
    else:
        data.pop(_KEY_SCAN_DIR, None)
    try:
        _write(data)
    except OSError:
        # 磁盘只读 / 权限不足 / 空间满：记住失败就算了，页面继续能用
        return


def _write(data: dict[str, object]) -> None:
    """原子写：先写同目录临时文件再 replace，避免断电留下半截 JSON。"""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ui_settings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
