"""把现场所有日志打包成**一个 txt 文件**，方便发给技术支持。

为什么需要：现场出问题时，日志分散在 logs\\lion-monitor.log、launcher.log、
server.log、selfcheck.log、service.log…… 让人一个个找、一个个发，
十有八九漏掉最关键的那个（2026-11-06：同事机器装完打开是 500，只捞到
launcher/selfcheck，缺的正是 server.log）。

0.1.2 起服务输出（Streamlit 启动横幅、uvicorn、print）统一进 lion-monitor.log，
server.log 只在 run.py 起不来时留兜底报错；这个脚本仍然全收，避免漏。

用法（安装目录下）：

    runtime\\python.exe tools\\collect_logs.py
    runtime\\python.exe tools\\collect_logs.py --root D:\\LionMonitor
    runtime\\python.exe tools\\collect_logs.py --tail 800      # 每个日志保留的行数

产物：桌面（优先）或安装目录下的 ``LionMonitor-诊断-<时间戳>.txt``，并把完整路径打印出来。
"""

from __future__ import annotations

import argparse
import os
import platform
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

#: 这些日志各保留一段尾部（大的截断，小的全留）
LOG_FILES = (
    "launcher.log",
    "server.log",
    "lion-monitor.log",
    "selfcheck.log",
    "service.log",
    "install_stop.log",
)


def _safe(text: str) -> str:
    """日志里混着 GBK/UTF-8 时不做假设，坏字节直接替换，绝不因为编码再崩一次。"""
    return text.encode("utf-8", "replace").decode("utf-8")


def _read_tail(path: Path, lines: int) -> str:
    if not path.is_file():
        return "（没有这个文件）"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError as exc:
        return f"（读不出来：{exc}）"
    if len(content) > lines:
        head = f"（共 {len(content)} 行，只保留最后 {lines} 行）\n"
        content = content[-lines:]
    else:
        head = f"（共 {len(content)} 行）\n"
    return head + "".join(content).rstrip()


def _http_status(port: int, path: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with opener.open(url, timeout=3) as resp:
            body = resp.read(120).decode("utf-8", "ignore").strip()
            return f"HTTP {resp.status}  {body[:80]!r}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}（异常响应）"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"连不上：{exc}"


def _port_owner(port: int, root: Path | None = None) -> str:
    """谁占着端口：netstat -ano + tasklist（都不依赖第三方库）。"""
    try:
        out = subprocess.run(  # noqa: S603,S607 - 固定命令，只为排障
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"（netstat 失败：{exc}）"
    marker = f":{port}"
    rows = [
        line.strip()
        for line in out.splitlines()
        if marker in line and "LISTENING" in line.upper()
    ]
    if not rows:
        return f"没有进程在监听 {port}"
    lines = ["监听记录："] + [f"  {row}" for row in rows]
    pids = {row.split()[-1] for row in rows}
    try:
        tasks = subprocess.run(  # noqa: S603,S607
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        tasks = ""
    for line in tasks.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1] in pids:
            lines.append(f"  进程：{parts[0]} pid={parts[1]}")

    # 可执行文件全路径：判断「占着端口的是不是本安装目录的 runtime\python.exe」
    pid_file = (root / "logs" / "server.pid") if root is not None else None
    if pid_file is not None and pid_file.is_file():
        try:
            text = pid_file.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff").strip()
            lines.append(f"启动器记录的 server.pid：{_safe(text)}")
        except OSError:
            pass
    return "\n".join(lines)


def _config_text(root: Path) -> str:
    cfg = root / ".streamlit" / "config.toml"
    if not cfg.is_file():
        return "（没有 .streamlit\\config.toml）"
    try:
        return cfg.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读不出来：{exc}）"


def _file_table(root: Path) -> str:
    _ROOT = Path(__file__).resolve().parents[1]
    if str(_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(_ROOT / "src"))
    from lion_monitor.deploy.selfcheck import REQUIRED_FILES

    rows = []
    for name in REQUIRED_FILES:
        path = root / name
        if path.is_file():
            rows.append(f"  ✅ {name:<58} {path.stat().st_size:>12,} B")
        else:
            rows.append(f"  ❌ {name:<58} 缺失")
    return "\n".join(rows)


def build_report(root: Path, tail: int, port: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    version = "?"
    try:
        version = (root / "VERSION").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass

    parts: list[str] = []
    parts.append("=" * 72)
    parts.append("Lion-Monitor 诊断日志")
    parts.append("=" * 72)
    parts.append(f"生成时间：{now}")
    parts.append(f"程序版本：{version}")
    parts.append(f"安装目录：{root}")
    parts.append(f"主机名　：{socket.gethostname()}    用户：{os.environ.get('USERNAME', '?')}")
    parts.append(f"系统　　：{platform.platform()}")
    parts.append(f"架构/编码：{platform.machine()}  {sys.getfilesystemencoding()}")
    parts.append(f"Python　：{sys.version.split()[0]}  ({sys.executable})")

    parts.append("")
    parts.append("--- 端口与实例 ---")
    parts.append(
        f"http://127.0.0.1:{port}/_stcore/health → " f"{_http_status(port, '/_stcore/health')}"
    )
    parts.append(f"http://127.0.0.1:{port}/             → {_http_status(port, '/')}")
    parts.append(_port_owner(port, root))

    parts.append("")
    parts.append("--- 关键文件完整性 ---")
    try:
        parts.append(_file_table(root))
    except Exception as exc:  # noqa: BLE001 - 采集脚本不能因为自身问题中断
        parts.append(f"（检查失败：{exc}）")

    parts.append("")
    parts.append("--- 服务配置 .streamlit\\config.toml ---")
    parts.append(_config_text(root))

    status = root / "logs" / "selfcheck.status"
    if status.is_file():
        parts.append("")
        parts.append("--- logs\\selfcheck.status ---")
        parts.append(_safe(status.read_text(encoding="utf-8", errors="replace").strip()))

    for name in LOG_FILES:
        parts.append("")
        parts.append(f"--- logs\\{name} ---")
        parts.append(_safe(_read_tail(root / "logs" / name, tail)))

    parts.append("")
    parts.append("=" * 72)
    parts.append("以上内容可直接转发给技术支持。")
    parts.append("=" * 72)
    return "\n".join(parts)


def _output_path(root: Path) -> Path:
    """优先放桌面（用户一眼能找到），桌面不存在就放安装目录。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"LionMonitor-诊断-{stamp}.txt"
    candidates = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidates.append(Path(profile) / "Desktop")
        onedrive = os.environ.get("OneDrive")
        if onedrive:
            candidates.append(Path(onedrive) / "Desktop")
    for folder in candidates:
        if folder.is_dir():
            return folder / name
    return root / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="收集 Lion-Monitor 全部日志到一个 txt")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=0, help="默认从 config.toml 的 port 读")
    parser.add_argument("--tail", type=int, default=400, help="每个日志保留最后多少行")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    port = args.port or 8501
    if not args.port:
        cfg = _config_text(root)
        for line in cfg.splitlines():
            if line.strip().startswith("port"):
                try:
                    port = int(line.split("=", 1)[1].strip())
                except (IndexError, ValueError):
                    pass

    try:
        report = build_report(root, max(50, args.tail), port)
    except Exception as exc:  # noqa: BLE001 - 兜底：至少给出一句人能看懂的话
        report = f"Lion-Monitor 诊断日志收集失败：{exc}\n安装目录：{root}\n"

    target = _output_path(root)
    try:
        target.write_text(report, encoding="utf-8-sig")
    except OSError as exc:
        print(f"写文件失败：{exc}")
        return 1

    print(f"已生成诊断日志：{target}")
    print(f"（{target.stat().st_size // 1024} KB，含 logs 里全部日志的末尾 {args.tail} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
