"""阶段 2 交付物的回归保护。

这些文件（Inno 脚本、C# 启动器、PowerShell 构建脚本）在 WSL 里编译不了，
所以用「结构性断言」把关键决定钉死 —— 谁误删了 `DisableDirPage=no`
或把 C# 写成了 csc.exe 不支持的新语法，测试会立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "installer" / "LionMonitor.iss"
LAUNCHER = ROOT / "launcher" / "LionMonitor.cs"
BUILD_PS1 = ROOT / "scripts" / "build_win.ps1"
BUILD = ROOT / "build.py"
REQ_WIN = ROOT / "requirements-win.txt"
SERVICE_INSTALL = ROOT / "installer" / "service_install.cmd"
SERVICE_REMOVE = ROOT / "installer" / "service_remove.cmd"
RELOCATE = ROOT / "tools" / "relocate_demo.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_csharp_comments(text: str) -> str:
    """去掉注释再查语法 —— 头部注释里正大光明地写着「不要用 $\"…\" 」，不能算违规。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


@pytest.mark.parametrize(
    "path",
    [
        ISS,
        LAUNCHER,
        BUILD,
        REQ_WIN,
        SERVICE_INSTALL,
        SERVICE_REMOVE,
        ROOT / "start.bat",
        ROOT / "stop.bat",
        ROOT / "run.py",
        ROOT / "tools" / "selfcheck.py",
        RELOCATE,
        ROOT / "installer" / "lion.ico",
        ROOT / "installer" / "lion-icon.png",
        ROOT / "app" / "assets" / "lion_icon.png",
    ],
)
def test_stage2_artifacts_exist(path: Path) -> None:
    assert path.exists(), f"阶段 2 产物缺失：{path.relative_to(ROOT)}"


# ---------------------------------------------------------------- 图标


def test_icon_is_multi_size_ico() -> None:
    """EXE/安装包图标必须带小尺寸，否则任务栏和资源管理器里会被缩放糊掉。"""
    from PIL import Image

    with Image.open(ROOT / "installer" / "lion.ico") as ico:
        sizes = set(ico.info.get("sizes", []))
    for need in ((16, 16), (32, 32), (48, 48), (256, 256)):
        assert need in sizes, f"lion.ico 缺 {need[0]}x{need[1]}，实际 {sorted(sizes)}"


def test_icon_wired_into_launcher_installer_and_page() -> None:
    assert "/win32icon:" in _read(BUILD), "build.py 没把 lion.ico 编进启动器"
    assert "SetupIconFile=lion.ico" in _read(ISS), "安装包没用自己的图标"
    assert "assets\" / \"lion_icon.png\"" in _read(ROOT / "app" / "main.py"), (
        "浏览器标签页图标没换成 lion_icon.png"
    )


# ---------------------------------------------------------------- 用户手册


def test_manual_is_bundled_with_shortcut() -> None:
    """用户手册要能开箱看到：开始菜单快捷方式 + 装进 {app}\\docs。"""
    manual = ROOT / "docs" / "manual" / "用户使用手册.pdf"
    if not manual.is_file():
        pytest.skip("用户手册只在构建机本地生成，源码仓库不含 docs/manual")
    assert manual.stat().st_size > 100_000, "手册 PDF 太小，可能只打印了空页"

    iss = _read(ISS)
    assert 'Name: "{group}\\用户使用手册"' in iss
    assert "{app}\\docs\\用户使用手册.pdf" in iss
    assert 'ROOT / "docs" / "manual"' in _read(BUILD), "build.py 没把手册拷进 dist\\docs"


def test_manual_covers_core_pages() -> None:
    """手册是给人看的：至少要覆盖导航里的每个页面，别写漏。"""
    manual = ROOT / "docs" / "manual" / "用户使用手册.md"
    if not manual.is_file():
        pytest.skip("用户手册只在构建机本地生成，源码仓库不含 docs/manual")
    text = manual.read_text(encoding="utf-8")
    for page in (
        "工作台",
        "任务列表",
        "健康树配置",
        "监测对象",
        "传感器",
        "指标配置",
        "阈值设置",
        "数据接入",
        "趋势分析",
        "数据集管理",
        "数据分析",
        "数据库备份",
        "回收站",
    ):
        assert page in text, f"用户手册没写「{page}」"
    for topic in ("安装与卸载", "判级口径", "常见问题", "术语表"):
        assert topic in text, f"用户手册缺少「{topic}」章节"


# ---------------------------------------------------------------- Inno 脚本


def test_iss_lets_user_pick_install_dir() -> None:
    """DisableDirPage=no 是「允许用户选目录」的开关，漏了会静默装默认路径。"""
    text = _read(ISS)
    assert "DisableDirPage=no" in text
    assert re.search(r"^DefaultDirName=\{sd\}\\LionMonitor$", text, re.M)
    assert "UsePreviousAppDir=no" in text, "升级时也要让用户重新选目录"


def test_iss_is_a_guided_wizard() -> None:
    """安装/卸载都要有向导页和下一步提示；小屏上向导要居中，否则浏览按钮会跑出屏幕。"""
    text = _read(ISS)
    assert "DisableWelcomePage=no" in text
    assert "DisableReadyPage=no" in text
    assert "DisableFinishedPage=no" in text
    assert 'InfoBeforeFile=安装说明.txt' in text
    assert 'InfoAfterFile=安装完成.txt' in text
    assert "WizardForm.Left := 100" in text
    assert "WizardForm.Top := 100" in text
    assert "CurPageChanged" in text
    for name in ("安装说明.txt", "安装完成.txt"):
        assert (ROOT / "installer" / name).is_file(), name
    assert "ConfirmUninstall=" in text
    assert "data、logs、backup" in text


def test_iss_requires_admin_for_firewall_and_service() -> None:
    text = _read(ISS)
    assert "PrivilegesRequired=admin" in text, "防火墙规则和服务注册都要管理员"
    assert "ArchitecturesAllowed=x64compatible" in text


def test_iss_keeps_user_data_on_uninstall() -> None:
    """卸载不能默认删库 —— 数据和备份要留着（§2）。"""
    text = _read(ISS)
    section = re.search(r"\[UninstallDelete\](.*?)(?=\n\[)", text, re.S)
    assert section, "缺少 [UninstallDelete] 段"
    body = section.group(1)
    for keep in ("data", "logs", "backup"):
        assert f'Name: "{{app}}\\{keep}"' not in body, f"卸载清单里不该出现 {keep}\\（要保留）"


def test_iss_and_selfcheck_agree_on_port_and_rule_name() -> None:
    iss = _read(ISS)
    assert "{#MyPort}" in iss and '"8501"' in iss
    assert 'FirewallRule "Lion-Monitor 8501"' in iss
    selfcheck = _read(ROOT / "src" / "lion_monitor" / "deploy" / "selfcheck.py")
    assert 'f"Lion-Monitor {port}"' in selfcheck, "自检和安装器的防火墙规则名必须一致"


def test_chinese_language_file_ships_with_installer() -> None:
    """Inno 6.7 默认不带 ChineseSimplified.isl，必须随 installer\\ 一起走。"""
    text = _read(ISS)
    assert 'MessagesFile: "ChineseSimplified.isl"' in text
    assert (ROOT / "installer" / "ChineseSimplified.isl").is_file()


def test_iss_runs_selfcheck_and_registers_service() -> None:
    text = _read(ISS)
    assert "tools\\selfcheck.py" in text
    assert "tools\\seed_demo.py" in text, "演示版安装包必须由 seed_demo 铺数据并重定位路径"
    assert "service_install.cmd" in text
    assert "service_remove.cmd" in text
    assert "[UninstallRun]" in text
    # 卸载必须先删服务，否则 runtime 里的 dll 被占用删不掉
    assert text.index("DelService") > text.index("DelFirewallRule")


# ---------------------------------------------------------------- C# 启动器


@pytest.mark.parametrize(
    "pattern, why",
    [
        (r'\$"', "字符串插值（C# 6）：csc.exe v4.0.30319 只认 C# 5"),
        (r"nameof\(", "nameof（C# 6）"),
        (r"\?\.", "空条件运算符 ?.（C# 6）"),
        (r"\bout\s+var\b", "out var（C# 7）"),
        (r"\bis\s+not\b", "is not（C# 9）"),
        (r"\brecord\s+\w+", "record（C# 9）"),
        (r"=>\s*\{?\s*$", "表达式体成员（C# 6/7），lambda 除外"),
    ],
)
def test_launcher_stays_csharp5(pattern: str, why: str) -> None:
    text = _strip_csharp_comments(_read(LAUNCHER))
    hits = [m.group(0) for m in re.finditer(pattern, text, re.M)]
    assert not hits, f"启动器用了 {why}，Windows 自带 csc.exe 编不过：{hits[:3]}"


def test_launcher_binds_all_interfaces_for_lan_access() -> None:
    """局域网多人访问（§12）：必须 0.0.0.0，不能用 127.0.0.1。"""
    text = _read(LAUNCHER)
    assert "--server.address 0.0.0.0" in text
    assert "--server.address 127.0.0.1" not in text


def test_launcher_prefers_edge_over_ie() -> None:
    """Server 2022 默认只有 IE11 —— 启动器必须优先 Edge/Chrome（§16）。"""
    text = _read(LAUNCHER)
    assert "msedge.exe" in text
    assert "chrome.exe" in text
    assert text.index("msedge.exe") < text.index("chrome.exe"), "Edge 要先试"


def test_launcher_probes_health_and_port_range() -> None:
    text = _read(LAUNCHER)
    assert "/_stcore/health" in text
    assert re.search(r"FirstPort = 8501", text)
    assert re.search(r"LastPort = 8510", text)
    assert "StartupTimeoutMs = 90000" in text, "Server 2022 冷启动慢，超时不能太短"


def test_launcher_detects_existing_instance_by_health_only() -> None:
    """Streamlit 1.66 首页是 SPA 空壳，靠 HTML 里的标题判断会永远判「没在跑」。"""
    text = _read(LAUNCHER)
    assert "IsOurApp" not in text, "单实例判断不能再依赖首页 HTML"
    section = text[text.index("FindRunningLion") : text.index("PortFree")]
    assert "IsHealthy" in section


def test_launcher_reports_failure_with_log_tail() -> None:
    """启动失败不能干瞪眼：要显示日志末尾 + 日志路径（§3）。"""
    text = _read(LAUNCHER)
    assert "ReadTail" in text
    assert "logs" in text
    assert "server.log" in text
    assert "launcher.log" in text


# ---------------------------------------------------------------- 构建脚本


def test_build_script_covers_all_steps() -> None:
    text = _read(BUILD)
    for step in (
        "requirements-win.txt",
        "csc.exe",
        "nssm",
        "ISCC.exe",
        "deps.lock",
        "selfcheck.py",
        "embed-amd64.zip",
    ):
        assert step in text, f"构建脚本缺少步骤：{step}"
    assert "BuildError" in text and "returncode != 0" in text, (
        "每步都要检查退出码，不能带着半成品继续"
    )


def test_build_script_works_without_github() -> None:
    """现场网络不通 GitHub：必须能自动回退 embeddable + 用离线 wheel 装依赖。"""
    text = _read(BUILD)
    assert "embed-amd64.zip" in text, "必须有本地运行时压缩包路径（GitHub 不通）"
    assert "get-pip.py" in text, "embeddable 没有 pip，必须引导 get-pip"
    assert "_pth" in text, "embeddable 必须修正 python3xx._pth"
    assert "--no-index" in text and "--find-links" in text, "要能全程离线装依赖"
    assert "--wheels-dir" in text
    assert "--runtime-archive" in text, "要能用本地运行时压缩包"


def test_build_script_supports_fast_rebuild() -> None:
    """软件几乎每天改界面 —— 必须能只换代码（跳过运行时和依赖）。"""
    text = _read(BUILD)
    assert "--skip-runtime" in text
    assert "--skip-deps" in text


def test_build_script_copies_streamlit_config() -> None:
    """缺少 .streamlit\\config.toml 时主题/端口配置会丢，界面变成默认亮色。"""
    text = _read(BUILD)
    assert '".streamlit"' in text


def test_build_script_verifies_imports_before_launcher() -> None:
    """核心依赖缺一个页面就起不来，必须在打包前中止。"""
    text = _read(BUILD)
    assert "def verify_imports" in text
    assert "verify_imports()" in text
    assert "streamlit" in text and "numba" in text


def test_build_script_supports_demo_installer() -> None:
    """演示版：带三批现场数据、安装时把绝对路径改到 {app}\\data\\四通道保存。"""
    text = _read(BUILD)
    assert "--demo-data" in text
    assert "--keep-data" in text
    assert "relocate_demo.py" in text
    assert "clean_build_artifacts(keep_data=" in text


# ---------------------------------------------------------------- 依赖清单


def test_requirements_win_has_core_and_extended() -> None:
    text = _read(REQ_WIN)
    for pkg in (
        "numpy",
        "pandas",
        "pydantic",
        "pyyaml",
        "scipy",
        "matplotlib",
        "streamlit",
        "plotly",
        "sqlalchemy",
    ):
        assert pkg in text, f"核心依赖缺 {pkg}"
    for pkg in ("PyWavelets", "statsmodels", "scikit-learn", "numba"):
        assert pkg.lower() in text.lower(), f"扩展算法依赖缺 {pkg}（§12 要求全预装）"


def test_requirements_win_matches_selfcheck_module_names() -> None:
    """自检里检查的模块名要和清单对得上，否则自检会误报。"""
    selfcheck = _read(ROOT / "src" / "lion_monitor" / "deploy" / "selfcheck.py")
    req = _read(REQ_WIN).lower()
    # pip 名 → import 名的映射
    mapping = {
        "pyyaml": "yaml",
        "pywavelets": "pywt",
        "scikit-learn": "sklearn",
    }
    for pip_name, import_name in mapping.items():
        assert f'"{import_name}"' in selfcheck, f"自检没检查 {import_name}"
        assert pip_name in req, f"清单里没有 {pip_name}"


# ---------------------------------------------------------------- 服务脚本


def test_service_installer_sets_env_and_autostart() -> None:
    text = _read(SERVICE_INSTALL)
    assert "SERVICE_AUTO_START" in text, "开机自启靠服务，不是 Startup 文件夹（§12）"
    assert "LION_LOG_DIR" in text and "LION_DB" in text
    assert "--server.address 0.0.0.0" in text
    assert "AppExit Default Restart" in text, "崩溃要能自动拉起"


def test_service_remover_falls_back_to_sc() -> None:
    """卸载时 runtime\\ 可能已被删，必须有 sc 兜底。"""
    text = _read(SERVICE_REMOVE)
    assert "sc stop LionMonitor" in text
    assert "sc delete LionMonitor" in text


def test_powershell_script_has_utf8_bom() -> None:
    """PowerShell 5.1 只有见到 BOM 才按 UTF-8 解析。

    没有 BOM 时它按系统 ANSI/GBK 读文件，中文注释会变成乱码字节，脚本一启动就语法错误退出
    —— 现场表现就是「双击 / 运行 PowerShell 闪退」（2026-11-05 实际踩到）。
    """
    data = BUILD_PS1.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), "scripts/build_win.ps1 必须保存为 UTF-8 with BOM"


def test_batch_files_use_crlf_and_utf8_no_bom() -> None:
    """批处理：CRLF 换行（cmd 要求），且不能带 BOM（老 cmd 会把 BOM 当命令的一部分）。"""
    for rel in (
        "build.bat",
        "start.bat",
        "stop.bat",
        "installer/service_install.cmd",
        "installer/service_remove.cmd",
    ):
        data = (ROOT / rel).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), f"{rel} 不能带 BOM"
        lf_only = data.count(b"\n") - data.count(b"\r\n")
        assert lf_only == 0, f"{rel} 有 {lf_only} 个裸 LF，cmd 会解析错"
        assert b"chcp 65001" in data, f"{rel} 需要 chcp 65001 才能正常显示中文"


def test_build_writes_transcript_log() -> None:
    """闪退时必须有日志可查（现场问「Log 在哪里」时它还一条都没写）。"""
    text = _read(BUILD)
    assert "open_log" in text and ".build" in text
    assert "build-" in text and ".log" in text
    assert "构建失败" in text, "失败要给出明确结论而不是静默退出"
    assert "完整日志" in text, "失败要告诉用户日志在哪"


def test_build_bat_does_not_depend_on_powershell() -> None:
    """现场实测：这台 Server 2022 上 powershell.exe 返回 0 却什么都不执行。

    构建报「成功」但 dist\\ 根本没生成 —— 所以 build.bat 必须直接调 python，不能走 PowerShell。
    """
    text = (ROOT / "build.bat").read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()
    # 注释里提到 powershell 是为了说明原因，这里只看真正会执行的命令行
    code = "\n".join(
        line for line in lowered.splitlines() if not line.strip().startswith(("rem", "::"))
    )
    assert "powershell" not in code, "build.bat 不能依赖 powershell（现场不可用）"
    assert "build.py" in lowered, "build.bat 要把活交给 build.py"
    assert "runtime" in lowered and "python.exe" in lowered
    assert "pause" in lowered, "双击运行时窗口必须停住，否则出错看不到"
    # 引导 python 的三步必须在 bat 里（还没 python 时只能靠 bat）
    assert "tar.exe" in lowered, "解压 embeddable 要靠 tar.exe"
    assert "get-pip.py" in lowered
    assert "import site" in lowered


def test_fix_pth_writes_usable_path(tmp_path: Path) -> None:
    r"""真跑 fix_pth，验证写出来的 _pth 内容（比字符串断言可靠）。

    _pth 里必须是单个反斜杠的 Lib\site-packages —— 写成双反斜杠 Python 就找不到
    这个目录，pip 装的包全都 import 不到，构建看起来成功但运行时报 ModuleNotFoundError。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("lionbuild", ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    fake_runtime = tmp_path / "runtime"
    fake_runtime.mkdir()
    pth = fake_runtime / "python311._pth"
    pth.write_text("python311.zip\r\n.\r\n", encoding="ascii")

    origin_runtime = module.RUNTIME
    module.RUNTIME = fake_runtime
    try:
        module.fix_pth()
    finally:
        module.RUNTIME = origin_runtime

    content = pth.read_bytes().decode("ascii")  # 必须读字节：read_text 会做通用换行转换
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    assert "python311.zip" in lines, "要保留 zip 入口"
    assert "Lib\\site-packages" in lines, f"site-packages 路径不对：{lines}"
    assert "import site" in lines, "没有 import site 就不会扫描 site-packages"
    assert "\\\\" not in content, f"出现双反斜杠：{content!r}"
    assert content.count("\r\n") >= 4, "_pth 必须是 CRLF（embeddable 在 Windows 上读）"


def test_iss_points_to_parent_dist() -> None:
    r"""Inno 的 Source 相对路径基于 .iss 所在目录（installer\）。

    .iss 在 installer\ 下，dist\ 在项目根 —— 写成 "dist\*" 会解析成 installer\dist\*，
    编译时报「文件不存在」。必须写 "..\dist\*"。
    """
    text = _read(ISS)
    assert 'Source: "..\\dist\\*"' in text, "打包源必须是 ..\\dist\\*"
    assert 'Source: "dist\\*"' not in text, "写成 dist\\* 会找不到文件"
