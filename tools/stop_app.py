"""安装/升级前停掉正在运行的 Lion-Monitor（**只用标准库**）。

为什么单独一个脚本、而且不 import 本项目的包：
升级时 `src\\` 可能正被覆盖，旧运行时也未必还能 import 我们的模块；这个脚本要能在
「程序已经半坏」的状态下照样把进程停掉。

它做三件事：
1. 读 ``logs\\server.pid``（启动器写）并核对进程名；
2. 探 ``/_stcore/health``，是 Streamlit 实例就按端口找 PID（netstat + tasklist + taskkill）；
3. 停服务：只用系统自带 ``sc stop``；
4. 等端口释放，把过程写进 ``logs\\install_stop.log``，退出码 0 = 端口已空。

**故意不调 nssm.exe**：NSSM 的清单是 requireAdministrator，非管理员调用会弹 UAC，
静默安装会卡在「nssm.exe 正在请求你的许可」。

只杀**确认是 python / LionMonitor** 的监听进程；端口上是别的程序时不动它，退出码 2。

用法（安装器 PrepareToInstall 调用；也可手动排障）：

    runtime\\python.exe tools\\stop_app.py --root D:\\LionMonitor --port 8501
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

KILLABLE = {"python.exe", "pythonw.exe", "lionmonitor.exe"}


def _safe_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        done = subprocess.run(  # noqa: S603 - 固定命令
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    raw = (done.stdout or b"") + (done.stderr or b"")
    # 有的系统命令输出 UTF-16（带 \x00），netstat/tasklist 用 OEM 代码页；两种都兜住
    if b"\x00" in raw:
        text = raw.decode("utf-16-le", "replace")
    else:
        text = raw.decode("mbcs", "replace")
    return done.returncode, text.replace("\x00", "")


def _health_ok(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=1.5) as resp:
            return resp.status == 200 and resp.read(16).decode("utf-8", "ignore").strip() == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _image_name(pid: str) -> str:
    code, out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
    if code != 0 or not out.strip():
        return ""
    first = out.strip().splitlines()[0]
    return first.split('","')[0].strip('"').lower()


def _listening_pid(port: int) -> str:
    code, out = _run(["netstat", "-ano"])
    if code != 0:
        return ""
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                return parts[-1]
    return ""


def stop(root: Path, port: int, log_path: Path) -> int:
    lines: list[str] = []

    def note(text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{stamp}] {text}")
        print(text)

    # 1) 启动器记录的 PID（同目录启动的实例）
    pid_file = root / "logs" / "server.pid"
    if pid_file.is_file():
        try:
            raw = pid_file.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff").strip()
            pid = raw.split()[0] if raw else ""
        except (OSError, IndexError):
            pid = ""
        if pid.isdigit():
            image = _image_name(pid)
            if image in KILLABLE:
                code, out = _run(["taskkill", "/PID", pid, "/T", "/F"])
                note(
                    f"按 logs\\server.pid 结束 pid={pid}（{image}）"
                    f"→ exit {code} {out.strip()[:80]}"
                )
            else:
                note(f"server.pid 里的 pid={pid} 已不是本程序（{image or '查不到'}），跳过")
        try:
            pid_file.unlink()
        except OSError:
            pass

    # 2) 端口上是不是 Streamlit 实例
    if _health_ok(port):
        pid = _listening_pid(port)
        if not pid:
            note(f"端口 {port} 有实例但查不到 PID，交给 sc stop 处理")
        else:
            image = _image_name(pid)
            if image in KILLABLE:
                code, out = _run(["taskkill", "/PID", pid, "/T", "/F"])
                note(f"结束占用 {port} 的进程 pid={pid}（{image}）→ exit {code} {out.strip()[:80]}")
            else:
                note(f"端口 {port} 被 {image or '未知程序'} 占用，不是本程序，不处理")
    else:
        note(f"端口 {port} 上没有运行中的 Lion-Monitor")

    # 3) 服务：只用 sc stop。
    #
    # 这里**故意不调 nssm.exe**：NSSM 的清单是 requireAdministrator，非管理员调用会弹
    # UAC 对话框——静默安装就卡死在那里（2026-11-06 实测：per-user 安装停在
    # 「nssm.exe 正在请求你的许可」）。sc stop 是系统自带命令，没权限时只是一句报错。
    code, out = _run(["sc", "stop", "LionMonitor"], timeout=40)
    note(f"sc stop LionMonitor → exit {code} {out.strip()[:80]}")

    # 4) 等端口释放（最多 20 秒）
    for _ in range(20):
        if _port_free(port):
            note(f"端口 {port} 已释放")
            break
        time.sleep(1)
    else:
        note(f"警告：端口 {port} 仍被占用")

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass

    return 0 if _port_free(port) else 1


def main(argv: list[str] | None = None) -> int:
    _safe_stdio()
    parser = argparse.ArgumentParser(description="停掉正在运行的 Lion-Monitor")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    port = args.port
    if not port:
        cfg = root / ".streamlit" / "config.toml"
        port = 8501
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("port"):
                    port = int(line.split("=", 1)[1].strip())
        except (OSError, IndexError, ValueError):
            pass

    print(f"停止 Lion-Monitor：安装目录 {root}，端口 {port}")
    return stop(root, port, root / "logs" / "install_stop.log")


if __name__ == "__main__":
    raise SystemExit(main())
