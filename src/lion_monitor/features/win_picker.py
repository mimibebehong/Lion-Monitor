"""弹 Windows 原生「浏览文件夹」对话框（2026-11-03 定）。

Streamlit 没有原生目录控件，网页也拿不到服务器路径，所以走服务端弹窗：
用仓库里的 ``win_pick_folder.cs``（首次使用时用 csc 编译）调 ``FolderBrowserDialog``
弹出系统自己的「浏览文件夹」（地址栏、左侧导航、新建文件夹都在），
并由后台线程把窗口设成 **TOPMOST**，不会被浏览器挡住。

注意：网页端已经改用 ``dir_browser``（局域网多人访问 / 服务模式不能用服务端弹窗），
本模块只保留给「单机本机排障」这类仍然需要原生对话框的场景。

约定：
- Windows 上直接用 ``%WINDIR%\\Microsoft.NET\\Framework64\\...\\csc.exe``；
- 宿主 exe 与结果文件放 ``%PUBLIC%\\lion_pick_bin``；
- 源码更新（mtime 变化）或 exe 缺失时自动重新编译；
- 结果由宿主以 UTF-16 写出，读的时候 ``encoding="utf-16"``；取消写 ``CANCELLED``。
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PickResult",
    "compile_helper",
    "helper_exe",
    "parse_output",
    "pick_folder",
    "picker_available",
    "windows_path_from_wsl",
    "wsl_path_from_windows",
]

IS_WINDOWS = os.name == "nt"


def _default_csc() -> Path:
    """Windows 自带的 C# 编译器；非 Windows（WSL 开发机）仍走 /mnt/c。"""
    if IS_WINDOWS:
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        for framework in ("Framework64", "Framework"):
            candidate = windir / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
            if candidate.is_file():
                return candidate
        return windir / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    return Path("/mnt/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")


#: Windows 侧的 C# 编译器（WSL 里通过 /mnt/c 调用）
CSC = _default_csc()
#: 宿主 exe、结果文件的家（两边都可写、路径不含空格）
SHARED_DIR = (
    Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    if IS_WINDOWS
    else Path("/mnt/c/Users/Public")
)
HELPER_DIR = SHARED_DIR / "lion_pick_bin"
HELPER_EXE_NAME = "LionPickFolder.exe"
#: 随仓库走的 C# 源码
SOURCE = Path(__file__).parent / "win_pick_folder.cs"
#: 对话框上方那行说明文字
TITLE = "选择数据目录"

_DRIVE = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")
_WSL_UNC = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+(\\|$)", re.IGNORECASE)


@dataclass(frozen=True)
class PickResult:
    """一次弹窗的结果：``path`` 是当前平台的路径，取消时 ``cancelled`` 为真。"""

    path: str | None = None
    cancelled: bool = False
    error: str | None = None


def windows_path_from_wsl(path: str | Path | None) -> str | None:
    """``/mnt/d/a/b`` → ``D:\\a\\b``；不是 Windows 盘挂载点则返回 None。"""
    if not path:
        return None
    text = str(path).replace("\\", "/").rstrip("/")
    match = _DRIVE.match(text)
    if not match:
        return None
    drive = match.group(1).upper()
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


def wsl_path_from_windows(path: str | None) -> str | None:
    """``D:\\a\\b`` → ``/mnt/d/a/b``；WSL 共享路径 ``\\\\wsl.localhost\\Ubuntu\\a`` → ``/a``。"""
    if not path:
        return None
    text = path.strip().strip('"')
    if not text:
        return None
    if text.upper() == "CANCELLED":
        return None
    if _WSL_UNC.match(text):
        # \\wsl.localhost\Ubuntu\home\a → /home/a
        parts = [p for p in text.split("\\") if p]
        rest = parts[2:]  # 去掉 \\wsl.localhost 与发行版名
        return "/" + "/".join(rest) if rest else "/"
    drive_match = re.match(r"^([a-zA-Z]):(.*)$", text)
    if not drive_match:
        return None
    drive = drive_match.group(1).lower()
    rest = drive_match.group(2).replace("\\", "/").strip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def picker_available() -> bool:
    """这台机器能不能编译并弹出 Windows 原生对话框（即能找到 csc.exe）。"""
    try:
        return CSC.is_file()
    except OSError:
        return False


def helper_exe(helper_dir: Path = HELPER_DIR) -> Path:
    """宿主 exe 的位置。"""
    return helper_dir / HELPER_EXE_NAME


def parse_output(text: str | None) -> str | None:
    """结果文件内容 → Windows 路径；取消或空内容返回 None。"""
    if not text:
        return None
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first or first.upper() == "CANCELLED":
        return None
    return first


def _to_windows_path(path: Path) -> str:
    """转成 csc 能用的 Windows 路径；Windows 上原样返回，WSL 下做 /mnt/c 换算。"""
    if IS_WINDOWS:
        return str(path)
    text = str(path)
    if text.startswith("/mnt/c"):
        text = "C:" + text[len("/mnt/c") :]
    return text.replace("/", "\\")


def compile_helper(
    *,
    compiler: Callable[..., object] | None = None,
    helper_dir: Path = HELPER_DIR,
    source: Path = SOURCE,
    csc: Path = CSC,
) -> Path | None:
    """把随仓库的 C# 源码编译成宿主 exe；exe 已是最新则直接复用。

    返回 exe 路径；编译失败返回 None。
    """
    exe = helper_exe(helper_dir)
    try:
        if exe.is_file() and source.is_file() and exe.stat().st_mtime >= source.stat().st_mtime:
            return exe
    except OSError:
        pass

    run = compiler or subprocess.run
    try:
        helper_dir.mkdir(parents=True, exist_ok=True)
        # 源码副本放共享目录再编译，避免某些环境对 \\wsl.localhost\ 路径处理不一致
        local_source = helper_dir / "win_pick_folder.cs"
        local_source.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return None

    try:
        run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                f"/out:{_to_windows_path(exe)}",
                "/r:System.Windows.Forms.dll",
                "/r:System.Drawing.dll",
                _to_windows_path(local_source),
            ],
            timeout=180,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if exe.is_file() else None


def pick_folder(
    initial: str | Path | None = None,
    *,
    title: str = TITLE,
    runner: Callable[..., object] | None = None,
    compiler: Callable[..., object] | None = None,
    timeout: float = 600.0,
    shared_dir: Path = SHARED_DIR,
    helper_dir: Path = HELPER_DIR,
) -> PickResult:
    """弹一次原生对话框并返回结果（``runner`` / ``compiler`` 只为测试注入）。"""
    if not picker_available():
        return PickResult(
            error="没能打开 Windows 的选目录窗口：找不到 csc.exe，请直接手填目录"
        )

    exe = compile_helper(compiler=compiler, helper_dir=helper_dir)
    if exe is None:
        return PickResult(error="没能准备好选目录程序（编译失败），请直接手填目录")

    run = runner or subprocess.run
    token = uuid.uuid4().hex[:12]
    out_local = shared_dir / f"lion_pick_{token}.txt"
    out_win = str(out_local) if IS_WINDOWS else f"C:\\Users\\Public\\lion_pick_{token}.txt"
    initial_win = str(initial) if IS_WINDOWS and initial else windows_path_from_wsl(initial) or ""

    raw = ""
    try:
        run(
            [str(exe), out_win, title, initial_win],
            timeout=timeout,
            check=False,
        )
        if out_local.exists():
            try:
                raw = out_local.read_text(encoding="utf-16")
            except OSError:
                raw = ""
    except subprocess.TimeoutExpired:
        return PickResult(error="选目录的窗口等待超时（10 分钟），请重试或直接手填目录")
    except OSError as exc:
        return PickResult(error=f"没能打开选目录窗口（{exc}），请直接手填目录")
    finally:
        try:
            out_local.unlink(missing_ok=True)
        except OSError:
            pass

    picked = parse_output(raw)
    if picked is None:
        return PickResult(cancelled=True)
    if IS_WINDOWS:
        return PickResult(path=picked)
    wsl_path = wsl_path_from_windows(picked)
    if wsl_path is None:
        return PickResult(error=f"选中的位置没法在服务器上访问：{picked}（要选 D: 这类本地盘）")
    return PickResult(path=wsl_path)
