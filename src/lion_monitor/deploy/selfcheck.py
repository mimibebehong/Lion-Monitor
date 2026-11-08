"""安装环境自检 —— 把「装了却打不开」变成装完就能发现的明确提示。

现场要求（2026-11-05）：
1. 报错都要落 log，方便日后运营排查；
2. 阶段 2 产物要在 Windows Server 2022 Standard / Win10 / Win11 三平台都能跑；
3. 「不要没反应」—— 每一项失败都要给一句人能看懂的处理办法。

用法：
    runtime\\python.exe -m lion_monitor.deploy.selfcheck --project-root C:\\LionMonitor
    runtime\\python.exe -m lion_monitor.deploy.selfcheck --json     # 给安装器解析

退出码：0 = 没有失败项（警告不算失败）；1 = 至少一项失败。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_STATUS_ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "⏭️"}
_STATUS_NAME = {OK: "通过", WARN: "警告", FAIL: "失败", SKIP: "跳过"}

#: 界面必需的核心依赖（缺任何一个页面就起不来）
CORE_MODULES = (
    "streamlit",
    "pandas",
    "numpy",
    "scipy",
    "plotly",
    "sqlalchemy",
    "yaml",
    "pydantic",
    "matplotlib",
)

#: 便携包里应当预装的算法依赖（缺失只影响用到这些算法的指标，不算装坏）
OPTIONAL_MODULES = ("pywt", "statsmodels", "sklearn", "numba")

#: 便携包解压后大约占多少（用于提示磁盘不足）
MIN_FREE_MB = 800


@dataclass(frozen=True)
class Check:
    """单项自检结果。``fix`` 必须是人话，安装器会原样显示给现场。"""

    key: str
    title: str
    status: str
    detail: str = ""
    fix: str = ""


# ---------------------------------------------------------------- 基础检查


def check_python(min_version: tuple[int, int] = (3, 10)) -> Check:
    info = sys.version_info
    current = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) < min_version:
        need = f"{min_version[0]}.{min_version[1]}"
        return Check(
            key="python",
            title="Python 运行时",
            status=FAIL,
            detail=f"当前 {current}，需要 ≥ {need}",
            fix=f"便携运行时版本不对：请用正式安装包（内含 {need}+ 运行时）重新安装。",
        )
    return Check(key="python", title="Python 运行时", status=OK, detail=current)


def _missing(modules: Sequence[str]) -> list[str]:
    return [m for m in modules if importlib.util.find_spec(m) is None]


def check_modules(modules: Sequence[str], label: str = "核心依赖") -> Check:
    missing = _missing(modules)
    if missing:
        return Check(
            key="core_modules",
            title=label,
            status=FAIL,
            detail="缺少 " + ", ".join(missing),
            fix=(
                "运行时缺少必需库，界面起不来。请重新运行安装包覆盖安装；"
                "若是自己装的 Python，执行 "
                "runtime\\python.exe -m pip install -r requirements-win.txt。"
            ),
        )
    return Check(key="core_modules", title=label, status=OK, detail=f"{len(modules)} 个全部可用")


def check_optional_modules(modules: Sequence[str] = OPTIONAL_MODULES) -> Check:
    missing = _missing(modules)
    if missing:
        return Check(
            key="optional_modules",
            title="扩展算法依赖",
            status=WARN,
            detail="缺少 " + ", ".join(missing),
            fix=(
                "用到小波/统计/机器学习类指标时才会报错。"
                "现场可让运维用 fetch_wheels.ps1 拷离线 wheel，"
                "或改用含全部依赖的正式安装包覆盖安装。"
            ),
        )
    return Check(
        key="optional_modules", title="扩展算法依赖", status=OK, detail=f"{len(modules)} 个全部可用"
    )


def check_writable(path: Path, label: str = "目录") -> Check:
    """真实写一个临时文件再删掉 —— 只有实测才可信（只读属性、域策略都会骗过 os.access）。"""
    key = "data_writable" if "数据" in label else "logs_writable"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            key=key,
            title=f"{label}可写",
            status=FAIL,
            detail=f"{path} 写不进去：{exc}",
            fix=(
                f"该目录不可写，数据库和日志会失败。换个用户有权限的安装目录（不要装在 "
                f"C:\\Program Files 或盘根），或以管理员身份把 {path} 授权给当前用户。"
            ),
        )
    return Check(key=key, title=f"{label}可写", status=OK, detail=str(path))


def check_sqlite(db_path: Path) -> Check:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _selfcheck_probe (v TEXT)")
            conn.execute("DELETE FROM _selfcheck_probe")
            conn.execute("INSERT INTO _selfcheck_probe VALUES ('ok')")
            row = conn.execute("SELECT v FROM _selfcheck_probe").fetchone()
            conn.execute("DROP TABLE _selfcheck_probe")
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return Check(
            key="sqlite",
            title="数据库可读写",
            status=FAIL,
            detail=f"{db_path} 打不开：{exc}",
            fix=(
                "数据库文件不可读写（常见原因：装在 Program Files、目录被只读、"
                "或 lion.db 被其它程序占用）。请换安装目录或关闭占用程序后重试。"
            ),
        )
    if not row or row[0] != "ok":
        return Check(
            key="sqlite",
            title="数据库可读写",
            status=FAIL,
            detail="写入后读不回来",
            fix="磁盘可能已满或文件系统异常，请检查磁盘空间。",
        )
    return Check(key="sqlite", title="数据库可读写", status=OK, detail=str(db_path))


def _http_status(port: int, path: str, timeout: float = 2.0) -> int:
    """探本机端口的 HTTP 状态码，连不上返回 0；500 就是「页面打不开」。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with opener.open(url, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也是「连上了」
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _our_health_ok(port: int) -> bool:
    """端口上跑的是不是 Streamlit：探 ``/_stcore/health``，返回 ok 才算。

    现场常见场景：装完之后自检又跑了一遍，或者用户先双击启动器再自检 ——
    端口被自己的服务占着，报「端口被占用」只会让人白折腾（2026-11-05 装到
    D:\\software\\LionMonitor 时就碰上了）。这里先探健康检查，能用就算通过。

    走 ``ProxyHandler({})``：现场机器常设 HTTP_PROXY，代理会把 localhost 也拦掉。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}/_stcore/health"
    try:
        with opener.open(url, timeout=1.0) as resp:
            body = resp.read(32).decode("utf-8", "ignore").strip()
            return resp.status == 200 and body == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def check_port(port: int, expect_stopped: bool = False) -> Check:
    """端口检查。

    ``expect_stopped=True`` 是**安装器**用的口径：装文件之前必须没有任何实例占着
    8501，否则会在「程序正在跑」的状态下覆盖文件，轻则版本混用，重则静态资源被替换
    一半、首页直接返回 500（2026-11-06 同事机器上踩到：装完打开是
    ``Internal Server Error``）。
    """
    if port > 0 and _our_health_ok(port):
        index_status = _http_status(port, "/")
        if expect_stopped:
            return Check(
                key="port",
                title=f"端口 {port}",
                status=FAIL,
                detail=(
                    f"{port} 上还有 Lion-Monitor 在运行（首页 HTTP {index_status}）。"
                    "安装前必须先把它停掉，否则会在运行中覆盖程序文件。"
                ),
                fix=(
                    "关闭所有 Lion-Monitor 窗口；如果装了开机自启服务，用管理员 CMD 执行："
                    f"sc stop LionMonitor；再执行 netstat -ano | findstr :{port} 确认端口已释放。"
                ),
            )
        if index_status != 200:
            return Check(
                key="port",
                title=f"端口 {port}",
                status=FAIL,
                detail=(
                    f"{port} 上的 Lion-Monitor 活着，但首页返回 HTTP {index_status}"
                    "（浏览器看到的是 Internal Server Error）。"
                ),
                fix=(
                    "这个实例已经坏了：先退出它（托盘/任务管理器结束 LionMonitor.exe 与"
                    " runtime\\python.exe，或 sc stop LionMonitor），再重新运行安装包做修复安装。"
                ),
            )
        return Check(
            key="port",
            title=f"端口 {port}",
            status=OK,
            detail=f"{port} 已有 Lion-Monitor 在运行（健康检查 200、首页 200）",
        )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Windows 的 SO_REUSEADDR 允许抢占已被监听的地址（bind 仍会成功），
        # 必须用 SO_EXCLUSIVEADDRUSE 才能真实反映「端口是否被占用」。
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as exc:
            return Check(
                key="port",
                title=f"端口 {port}",
                status=FAIL,
                detail=f"端口 {port} 被占用或不允许绑定：{exc}",
                fix=(
                    f"端口 {port} 已被别的程序占用（也可能是上一份 Lion-Monitor 还在跑）。"
                    f"在命令行执行 netstat -ano | findstr :{port} 看是哪个进程，"
                    f"或关掉旧的 Lion-Monitor 后重试。"
                ),
            )
    return Check(key="port", title=f"端口 {port}", status=OK, detail=f"{port} 可用")


#: 程序文件完整性：缺一个就打不开（首页 500 最常见的来源就是静态资源/入口缺失）
REQUIRED_FILES = (
    "LionMonitor.exe",
    "run.py",
    "app/main.py",
    "app/nav_state.py",
    "app/ui.py",
    "app/assets/lion_icon.png",
    "src/lion_monitor/store/db.py",
    "runtime/python.exe",
    "runtime/Lib/site-packages/streamlit/static/index.html",
    "runtime/Lib/site-packages/streamlit/__init__.py",
    ".streamlit/config.toml",
)


def check_program_files(project_root: Path) -> Check:
    """程序文件完整性 —— 安装被中断/被杀软删文件时，这条会立刻报出来。"""
    missing = [name for name in REQUIRED_FILES if not (project_root / name).is_file()]
    if missing:
        shown = "、".join(missing[:5])
        tail = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
        return Check(
            key="program_files",
            title="程序文件",
            status=FAIL,
            detail=f"缺文件：{shown}{tail}",
            fix=(
                "程序没装完整：在「程序和功能」里卸载后重新安装，或直接再运行一次安装包"
                "（升级安装会补齐文件）。装之前先退出所有 Lion-Monitor 窗口。"
            ),
        )
    return Check(
        key="program_files",
        title="程序文件",
        status=OK,
        detail=f"{len(REQUIRED_FILES)} 个关键文件齐全",
    )


def check_runtime_import(project_root: Path) -> Check:
    """真把 streamlit 导一次 —— 缺文件/版本不匹配，只有 import 才会暴露。

    来历（2026-11-06）：``check_modules`` 用的是 ``find_spec``，只看包在不在、
    不真正执行，所以「plotly 装了一半」这种状态它判绿；结果是安装成功、自检全绿，
    浏览器打开却是 ``Internal Server Error``（streamlit 导入 plotly 主题时 AttributeError）。
    这里用子进程真导入，超时/异常都算失败，并直接给出修复办法。
    """
    python = project_root / "runtime" / "python.exe"
    exe = str(python) if python.is_file() else sys.executable
    code = "import streamlit, plotly; print(streamlit.__version__, plotly.__version__)"
    try:
        done = subprocess.run(  # noqa: S603 - 固定命令
            [exe, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=str(project_root),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            key="runtime_import",
            title="运行库导入",
            status=FAIL,
            detail=f"起不来 python：{exc}",
            fix="确认 runtime\\python.exe 没被杀软隔离；必要时重新运行安装包修复。",
        )

    if done.returncode == 0:
        return Check(
            key="runtime_import",
            title="运行库导入",
            status=OK,
            detail=f"streamlit / plotly 可导入（{done.stdout.strip()[:40]}）",
        )

    tail = [line.strip() for line in (done.stderr or "").strip().splitlines() if line.strip()]
    detail = tail[-1][:160] if tail else f"退出码 {done.returncode}"
    return Check(
        key="runtime_import",
        title="运行库导入",
        status=FAIL,
        detail=f"streamlit 导入失败：{detail}",
        fix=(
            "运行库文件不完整（常见于安装时旧实例没停、被覆盖了一半，或被杀软删了文件）。"
            "先退出所有 Lion-Monitor，再运行安装包做修复安装；仍失败请开始菜单 →"
            "「收集诊断日志（发给技术支持）」，把生成的 txt 发过来。"
        ),
    )


def check_scan_dir(path: Path | None) -> Check:
    if path is None:
        return Check(
            key="scan_dir", title="现场数据目录", status=SKIP, detail="未配置（页面里再选）"
        )
    if not path.is_dir():
        return Check(
            key="scan_dir",
            title="现场数据目录",
            status=FAIL,
            detail=f"{path} 不存在或不是目录",
            fix="在「数据接入」页用「浏览目录」重新选一个存在的目录；网络盘请确认已连接。",
        )
    try:
        subdirs = [p for p in path.iterdir() if p.is_dir()]
    except OSError as exc:
        return Check(
            key="scan_dir",
            title="现场数据目录",
            status=FAIL,
            detail=f"列不出来：{exc}",
            fix="可能没有读取权限，或网络盘无响应。请确认权限后重新选择目录。",
        )
    return Check(
        key="scan_dir",
        title="现场数据目录",
        status=OK,
        detail=f"{path}（{len(subdirs)} 个子目录）",
    )


def check_disk(path: Path, min_free_mb: int = MIN_FREE_MB) -> Check:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.anchor or "/")
    except OSError as exc:
        return Check(
            key="disk",
            title="磁盘空间",
            status=WARN,
            detail=f"读不到：{exc}",
            fix="不影响使用，但请确认磁盘剩余空间足够（约 800 MB）。",
        )
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        return Check(
            key="disk",
            title="磁盘空间",
            status=FAIL,
            detail=f"仅剩 {free_mb} MB",
            fix=(
                f"至少需要 {min_free_mb} MB 剩余空间"
                "（数据库、日志、备份都要放这里），请清理磁盘后重试。"
            ),
        )
    return Check(key="disk", title="磁盘空间", status=OK, detail=f"剩余 {free_mb} MB")


# ---------------------------------------------------------------- 平台相关


def _is_windows() -> bool:
    return os.name == "nt"


def _winreg():
    """Windows 注册表模块（Linux 上不存在，用动态导入避免导入期报错）。"""
    import importlib

    return importlib.import_module("winreg")


def find_browsers() -> list[str]:
    """返回本机找到的 Edge / Chrome 路径（Server 2022 默认可能只有 IE，必须查）。"""
    found: list[str] = []
    if _is_windows():
        winreg = _winreg()

        candidates = (
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe", "Edge"),
            (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe", "Chrome"),
        )
        for subkey, name in candidates:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        if value and Path(value).exists():
                            found.append(f"{name}: {value}")
                except OSError:
                    continue
        for guess in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ):
            if Path(guess).exists():
                found.append(f"{Path(guess).stem}: {guess}")
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser", "msedge"):
            located = shutil.which(exe)
            if located:
                found.append(f"{exe}: {located}")
    # 去重（注册表和猜测路径可能指同一处）
    return list(dict.fromkeys(found))


def check_browser() -> Check:
    found = find_browsers()
    if found:
        return Check(key="browser", title="浏览器", status=OK, detail="；".join(found[:2]))
    fix = (
        "本机没有 Edge/Chrome。界面是网页版，Streamlit 不支持 IE —— "
        "Windows Server 2022 默认只有 IE11，请先安装 Microsoft Edge，再打开 Lion-Monitor。"
    )
    if not _is_windows():
        fix = "当前系统没找到 Chrome/Chromium；部署到 Windows 时请确认装有 Edge 或 Chrome。"
    return Check(key="browser", title="浏览器", status=WARN, detail="未找到 Edge/Chrome", fix=fix)


def check_desktop_session() -> Check:
    """Server Core 没有桌面 —— 只能远程浏览器访问，要提前说清楚。"""
    if not _is_windows():
        return Check(key="desktop_session", title="桌面环境", status=SKIP, detail="非 Windows")
    winreg = _winreg()

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            install_type, _ = winreg.QueryValueEx(key, "InstallationType")
    except OSError:
        install_type = "unknown"
    if "core" in str(install_type).lower():
        return Check(
            key="desktop_session",
            title="桌面环境",
            status=WARN,
            detail="Server Core（无桌面）",
            fix="本机没有桌面，只能从别的电脑用浏览器访问 http://<本机IP>:8501，本机不会弹出浏览器窗口。",
        )
    return Check(key="desktop_session", title="桌面环境", status=OK, detail=str(install_type))


def check_firewall(port: int) -> Check:
    """局域网多人访问要放行端口；服务器三档 profile 默认都拦。"""
    if not _is_windows():
        return Check(key="firewall", title="防火墙放行", status=SKIP, detail="非 Windows")
    import subprocess

    rule_name = f"Lion-Monitor {port}"
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="mbcs",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            key="firewall",
            title="防火墙放行",
            status=WARN,
            detail=f"查不了：{exc}",
            fix=f"请手动在「高级安全 Windows Defender 防火墙」入站规则里放行 TCP {port}。",
        )
    if proc.returncode != 0 or "No rules match" in proc.stdout:
        return Check(
            key="firewall",
            title="防火墙放行",
            status=WARN,
            detail=f"没有「{rule_name}」规则",
            fix=(
                f"别的电脑可能打不开界面。以管理员身份执行："
                f'netsh advfirewall firewall add rule name="{rule_name}" '
                f"dir=in action=allow protocol=TCP localport={port}"
            ),
        )
    return Check(key="firewall", title="防火墙放行", status=OK, detail=rule_name)


def local_ipv4() -> list[str]:
    """列出可用于局域网访问的本机 IPv4（不依赖第三方库）。"""
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # 不真的发包，只为拿到出口网卡地址
            ips.add(str(sock.getsockname()[0]))
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = str(info[4][0])
            if not addr.startswith("127."):
                ips.add(addr)
    except OSError:
        pass
    return sorted(ips)


# ---------------------------------------------------------------- 汇总


def run_selfcheck(
    project_root: Path,
    port: int = 8501,
    scan_dir: Path | None = None,
    expect_stopped: bool = False,
) -> list[Check]:
    """按安装顺序跑一遍。``port=0`` 表示让系统挑端口（仅测试用）。

    ``expect_stopped=True`` 给安装器用：装文件前不允许有实例占着端口。
    """
    root = Path(project_root)
    checks: list[Check] = [
        check_python(),
        check_modules(CORE_MODULES),
        check_optional_modules(),
        check_program_files(root),
        check_runtime_import(root),
        check_writable(root / "data", label="数据目录"),
        check_writable(root / "logs", label="日志目录"),
        check_sqlite(root / "data" / "lion.db"),
        check_port(port, expect_stopped=expect_stopped),
        check_scan_dir(scan_dir),
        check_browser(),
        check_desktop_session(),
        check_firewall(port),
        check_disk(root),
    ]
    return checks


def summarize(checks: Sequence[Check], port: int = 8501) -> tuple[int, str]:
    counts = {status: 0 for status in (OK, WARN, FAIL, SKIP)}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1

    lines = ["Lion-Monitor 环境自检", "=" * 60]
    for check in checks:
        icon = _STATUS_ICON.get(check.status, "?")
        lines.append(f"{icon} {check.title:<16} {check.detail}")
        if check.status in {WARN, FAIL} and check.fix:
            lines.append(f"   → {check.fix}")

    ips = local_ipv4()
    if ips:
        lines.append("-" * 60)
        lines.append("局域网访问地址（同事在浏览器里打开）：")
        for ip in ips:
            lines.append(f"   http://{ip}:{port}")

    lines.append("-" * 60)
    lines.append(
        f"结果：{counts[OK]} 项通过 · {counts[WARN]} 项警告 · "
        f"{counts[FAIL]} 项失败 · {counts[SKIP]} 项跳过"
    )
    exit_code = 1 if counts[FAIL] else 0
    return exit_code, "\n".join(lines)


def write_log(project_root: Path, text: str) -> Path | None:
    """自检结论同时落盘 —— 现场只看弹窗、事后要能查。"""
    try:
        log_dir = Path(project_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "selfcheck.log"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{stamp}] {platform.platform()}\n{text}\n")
        return log_file
    except OSError:
        return None


def write_status(project_root: Path, exit_code: int) -> Path | None:
    """给安装器留一个只有 0/1 的短文件，避免它去猜日志内容。"""
    try:
        log_dir = Path(project_root) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        status_file = log_dir / "selfcheck.status"
        status_file.write_text(f"{exit_code}\n", encoding="ascii")
        return status_file
    except OSError:
        return None


def _safe_stdio() -> None:
    """控制台是 GBK 时打印 ✅/❌ 会抛 UnicodeEncodeError：宁可显示成 ? 也不能崩。

    装完在中文 cmd 里手动跑一次自检是现场常规动作，不能因为一个图标就报错退出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # 被重定向到不支持 reconfigure 的对象
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _safe_stdio()
    parser = argparse.ArgumentParser(description="Lion-Monitor 安装环境自检")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--scan-dir", default="")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给安装器解析）")
    parser.add_argument("--no-log", action="store_true", help="不写 logs/selfcheck.log")
    parser.add_argument(
        "--expect-stopped",
        action="store_true",
        help="安装器口径：装文件前端口上不能有实例在跑（有就判失败）",
    )
    args = parser.parse_args(argv)

    project_root = Path(args.project_root)
    scan_dir = Path(args.scan_dir) if args.scan_dir else None
    checks = run_selfcheck(
        project_root=project_root,
        port=args.port,
        scan_dir=scan_dir,
        expect_stopped=args.expect_stopped,
    )
    exit_code, text = summarize(checks, port=args.port)

    log_file = None if args.no_log else write_log(project_root, text)
    if not args.no_log:
        write_status(project_root, exit_code)

    if args.json:
        payload = {
            "exit_code": exit_code,
            "log_file": str(log_file) if log_file else "",
            "checks": [asdict(check) for check in checks],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        if log_file:
            print(f"\n自检记录：{log_file}")
    return exit_code
