"""安装期「先停旧实例」与「一键收日志」的回归保护。

背景（2026-11-06）：发布版在同事电脑上装完打开是 ``Internal Server Error``。
自检日志里有决定性证据——安装时 8501 上「已有 Lion-Monitor 在运行」，
说明旧实例没停就覆盖了程序文件。所以：

1. 安装器必须在**复制文件之前**停实例（Inno 的 ``PrepareToInstall``）；
2. 停实例的脚本不能依赖 PowerShell/WMI（现场见过 CIM 直接失灵的机器）；
3. 出问题时能用一条命令把全部日志收成一个 txt。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "installer" / "LionMonitor.iss"
STOP_APP = ROOT / "tools" / "stop_app.py"
COLLECT = ROOT / "tools" / "collect_logs.py"
for extra in (ROOT / "src", ROOT / "tools", ROOT / "app"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import collect_logs  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 安装前停实例


def test_iss_stops_instance_before_copying_files() -> None:
    text = _read(ISS)
    assert "function PrepareToInstall(" in text, "没有 PrepareToInstall，装文件前不会停实例"
    assert "StopRunningInstance();" in text
    # PrepareToInstall 必须真的被 Inno 调用，而不是只定义
    assert text.index("function PrepareToInstall(") > text.index("procedure StopRunningInstance();")
    assert "ExtractTemporaryFile('stop_app.py')" in text
    assert "stop_app.py" in text.split("[Files]")[1].split("[Dirs]")[0], "stop_app.py 没打进安装包"


def test_installer_selfcheck_requires_stopped_port() -> None:
    text = _read(ISS)
    assert "--expect-stopped" in text, "安装期自检必须要求端口已释放，否则覆盖运行中的文件"


def test_first_install_still_stops_instance_from_other_dir() -> None:
    """首次装到新目录时 {app}\\runtime 还不存在，跑不了 stop_app.py。

    实测（2026-11-06 18:06）：旧实例从 dist\\ 占着 8501，装到 D:\\lmfinal 时
    只 taskkill LionMonitor.exe 管不到真正监听的 python.exe，装完仍是 500。
    必须有一条纯 Inno 的「按端口结束进程」兜底。
    """
    text = _read(ISS)
    assert "function StopByPort(" in text
    assert "netstat -ano" in text
    assert "tasklist /FI" in text
    assert "taskkill.exe" in text
    # 兜底必须在首次安装分支里被调用，且卸载分支也有
    code = text.split("[Code]", 1)[1]
    assert code.count("StopByPort({#MyPort}") >= 2
    first_install = code.split("if not Stopped then", 1)[1].split("Sleep(1500)", 1)[0]
    assert "StopByPort({#MyPort}" in first_install
    assert "pythonw.exe" in code and "lionmonitor.exe" in code, "必须核对映像名，别误杀别的程序"


def test_custom_dialogs_are_silent_safe() -> None:
    """自定义 MsgBox 在 /VERYSILENT 下必须自动应答，否则 IT 批量部署会卡死。

    实测：v0.1.1 首次构建的「确认装到这个目录？」用的是 MsgBox，
    /VERYSILENT 安装直接停在那一步，等人工点「是」。
    """
    text = _read(ISS)
    code = text.split("[Code]", 1)[1]
    assert "SuppressibleMsgBox" in code
    # "MsgBox(" 会匹配到 "SuppressibleMsgBox("，要按词边界查真正的裸调用
    import re

    bare = re.findall(r"(?<![A-Za-z])MsgBox\(", code)
    assert not bare, "还有裸 MsgBox：静默安装会卡住"
    for line in code.splitlines():
        if "SuppressibleMsgBox(" in line and "mbError" in line:
            assert ", ID" in line or "IDOK" in line, f"缺静默默认按钮：{line.strip()}"


def test_stop_app_avoids_powershell_wmi() -> None:
    """现场机器上 Get-CimInstance 直接返回空，靠它停进程等于没停。"""
    text = _read(STOP_APP)
    assert "Get-CimInstance" not in text
    assert "powershell" not in text.lower()
    for tool in ("netstat", "tasklist", "taskkill"):
        assert tool in text, f"stop_app.py 少了 {tool}"


def test_stop_app_never_launches_nssm() -> None:
    """nssm.exe 清单是 requireAdministrator：非管理员调用会弹 UAC，静默安装直接卡死。

    实测 2026-11-06：/VERYSILENT 安装停在「nssm.exe 正在请求你的许可」。
    停服务只用系统自带 sc。
    """
    text = _read(STOP_APP)
    code = text.split('"""', 2)[-1]  # 去掉模块 docstring，注释里的说明不算调用
    for line in code.splitlines():
        code_line = line.split("#", 1)[0]
        assert "nssm" not in code_line.lower(), f"stop_app.py 不能执行 nssm：{line.strip()}"
    assert '"sc", "stop", "LionMonitor"' in text


def test_stop_app_only_kills_known_images() -> None:
    text = _read(STOP_APP)
    assert "KILLABLE" in text
    assert "python.exe" in text and "lionmonitor.exe" in text.lower()
    # 遇到别的程序占端口时必须放手，而不是见谁杀谁
    assert "不是本程序，不处理" in text


def test_stop_app_refuses_foreign_listener() -> None:
    """单元级验证：不是 python/LionMonitor 的镜像名 ⇒ 不杀（返回空 pid 处理）。"""
    import stop_app

    assert stop_app.KILLABLE == {"python.exe", "pythonw.exe", "lionmonitor.exe"}
    assert "nginx.exe" not in stop_app.KILLABLE


def test_launcher_records_pid_for_stop_script() -> None:
    text = _read(ROOT / "launcher" / "LionMonitor.cs")
    assert "server.pid" in text, "启动器没写 pid 文件，stop_app 只能靠端口猜"
    assert "new UTF8Encoding(false)" in text, "pid 文件不能带 BOM，否则停进程时解析失败"
    assert "LionMonitor.exe" in text


def test_uninstall_stops_instance_first() -> None:
    """卸载时程序还在跑，Inno 会跳过被占用的文件，留下半个目录。"""
    text = _read(ISS)
    assert "function InitializeUninstall(" in text
    assert "stop_app.py" in text.split("function InitializeUninstall(")[1][:600]


def test_service_scripts_never_launch_nssm_blindly() -> None:
    """停/卸服务不能盲调 nssm.exe：它要提权，非管理员一调就弹 UAC 卡死。

    - ``service_remove.cmd``：只用 sc（NSSM 注册的服务也能 sc delete）；
    - ``service_install.cmd``：允许用 nssm（注册服务本来就要管理员），
      但必须先 ``net session`` 判权，非管理员直接跳过。
    """
    remove = _read(ROOT / "installer" / "service_remove.cmd").lower()
    for line in remove.splitlines():
        code = line.split("rem", 1)[0]
        assert "nssm" not in code, f"service_remove.cmd 还在调 nssm：{line.strip()}"
    assert "sc delete lionmonitor" in remove

    install = _read(ROOT / "installer" / "service_install.cmd").lower()
    assert "net session" in install, "service_install.cmd 应先判断是不是管理员"
    assert "跳过服务注册" in install
    # 判权必须出现在调用 nssm 之前
    assert install.index("net session") < install.index('"%nssm%" install')


def test_uninstall_keeps_data_but_cleans_program_dirs() -> None:
    """文档承诺「卸载保留 data / logs / backup」——安装脚本必须真的这么做。

    做法：``data\\`` 里的东西一律不由 [Files] 记录（演示数据装在 ``demo_data\\``，
    装完由 tools\\seed_demo.py 新建到 ``data\\``），卸载器不认识 → 不会删。

    排除名必须是 ``demo_data`` 而不是 ``data``：Inno 的 ``Excludes`` 按「任意一段
    路径」匹配，写 ``data\\*`` 会把 plotly 的 ``graph_objs\\layout\\template\\data\\*``
    也排掉，装完 streamlit 直接 ImportError、首页 500（2026-11-06 实测）。
    """
    text = _read(ISS)
    assert 'Excludes: "demo_data\\*,data\\lion.db*"' in text
    assert 'Excludes: "data\\*"' not in text, "Excludes 写 data\\* 会误伤 plotly"
    assert 'DestDir: "{app}\\data"' not in text, "不能再用 [Files] 把数据装进 data\\"
    for name in ("docs", "launcher", ".streamlit"):
        assert f'Type: filesandordirs; Name: "{{app}}\\{name}"' in text, f"{name} 卸载后没清理"


def test_service_step_requires_admin_mode() -> None:
    """per-user 安装时 [Run] 里不能执行服务注册。"""
    text = _read(ISS)
    tail = text.split("Tasks: autostart", 1)
    assert len(tail) == 2, "找不到服务注册的 [Run] 行"
    assert "IsAdminInstallMode" in tail[1][:120], "服务注册那行必须加 Check: IsAdminInstallMode"


# ---------------------------------------------------------------- 首页 500 检测


def test_launcher_checks_index_not_only_health() -> None:
    """健康检查 200 但首页 500 时，直接开浏览器就是给用户看 Internal Server Error。"""
    text = _read(ROOT / "launcher" / "LionMonitor.cs")
    assert "IndexStatus" in text
    assert "BuildBrokenMessage" in text
    assert "500" in text


# ---------------------------------------------------------------- 一键收日志


def test_collect_logs_writes_single_file(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "app"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "launcher.log").write_text("启动器日志一行\n", encoding="utf-8")
    (root / "logs" / "server.log").write_text("服务日志一行\n", encoding="utf-8")
    (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("OneDrive", raising=False)

    code = collect_logs.main(["--root", str(root), "--tail", "50", "--port", "1"])

    assert code == 0
    files = list(desktop.glob("LionMonitor-诊断-*.txt"))
    assert len(files) == 1, "必须只产出一个文件"
    text = files[0].read_text(encoding="utf-8-sig")
    assert "服务日志一行" in text and "启动器日志一行" in text
    assert "Lion-Monitor 诊断日志" in text
    assert "关键文件完整性" in text


def test_collect_logs_survives_broken_root(tmp_path: Path, monkeypatch) -> None:
    """目录残缺也要出文件（现场可能就是半装状态）。"""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("OneDrive", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()

    assert collect_logs.main(["--root", str(empty), "--port", "1"]) == 0
    assert list(desktop.glob("LionMonitor-诊断-*.txt"))


def test_collect_logs_shortcut_in_installer() -> None:
    text = _read(ISS)
    assert "collect_logs.cmd" in text
    assert "收集诊断日志" in text, "开始菜单里没有「收集诊断日志」入口"
    assert (ROOT / "installer" / "collect_logs.cmd").is_file()
