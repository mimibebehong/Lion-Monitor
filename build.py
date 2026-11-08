"""Lion-Monitor Windows 构建（纯 Python，不依赖 PowerShell）。

为什么不用 build_win.ps1：现场这台 Server 2022 上 `powershell.exe` 返回 0 却什么都不执行
（连 `Write-Host HELLO` 都没有输出，也不产出任何文件），构建报「成功」但 dist\\ 根本没生成。
所以构建逻辑改成：

    build.bat  负责「先造出一个 python」（解压 embeddable + 引导 pip）
    build.py   本文件，用那个 python 跑完全部构建步骤

用法（在 Windows 上，双击 build.bat 即可）：
    dist\\runtime\\python.exe build.py
    dist\\runtime\\python.exe build.py --skip-runtime --skip-deps     只换代码，最快
    dist\\runtime\\python.exe build.py --no-installer                只到 dist\\，不打安装包

日志：.build\\build-<时间戳>.log（同时输出到控制台，失败也会保留）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
RUNTIME = DIST / "runtime"
WORK = ROOT / ".build"

IS_WINDOWS = os.name == "nt"

_log_handle = None
LOG_FILE: Path | None = None


# ---------------------------------------------------------------- 日志


def log(message: str = "", *, level: str = "info") -> None:
    prefix = {"step": "\n==> ", "warn": "⚠ ", "error": "❌ ", "info": ""}.get(level, "")
    line = f"{prefix}{message}"
    print(line, flush=True)
    if _log_handle is not None:
        _log_handle.write(line + "\n")
        _log_handle.flush()


def open_log() -> Path:
    global _log_handle, LOG_FILE
    WORK.mkdir(parents=True, exist_ok=True)
    LOG_FILE = WORK / f"build-{datetime.now():%Y%m%d-%H%M%S}.log"
    _log_handle = LOG_FILE.open("w", encoding="utf-8")
    _log_handle.write(f"Lion-Monitor 构建 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    _log_handle.write(f"python={sys.version} platform={sys.platform} cwd={os.getcwd()}\n\n")
    _log_handle.flush()
    return LOG_FILE


def close_log() -> None:
    global _log_handle
    if _log_handle is not None:
        _log_handle.close()
        _log_handle = None


class BuildError(RuntimeError):
    """构建中途失败，消息直接给人看。"""


def run(
    cmd: list[Path | str],
    *,
    what: str,
    cwd: Path | None = None,
    timeout: int = 1800,
    stream: bool = False,
) -> str:
    """跑一条外部命令：输出同时进日志，非零退出码就抛 BuildError（带命令和尾部输出）。

    ``stream=True`` 时逐行实时输出（pip 装几十个包要好几分钟，缓冲到结束会显得像卡死）。
    """
    log(f"$ {' '.join(str(c) for c in cmd)}")
    if stream:
        return _run_streaming(cmd, what=what, cwd=cwd, timeout=timeout)
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError as exc:
        raise BuildError(f"{what} 启动失败：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"{what} 超时（{timeout} 秒没跑完）") from exc

    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        for line in out.rstrip().splitlines():
            log(f"   {line}")
    if proc.returncode != 0:
        tail = "\n".join(out.rstrip().splitlines()[-15:])
        raise BuildError(f"{what} 失败（退出码 {proc.returncode}）：\n{tail}")
    return out


def _run_streaming(
    cmd: list[Path | str], *, what: str, cwd: Path | None = None, timeout: int
) -> str:
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            cwd=str(cwd or ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise BuildError(f"{what} 启动失败：{exc}") from exc

    collected: list[str] = []
    assert proc.stdout is not None
    started = time.time()
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        collected.append(line)
        log(f"   {line}")
        if time.time() - started > timeout:
            proc.kill()
            raise BuildError(f"{what} 超时（{timeout} 秒没跑完）")
    proc.wait()
    out = "\n".join(collected)
    if proc.returncode != 0:
        tail = "\n".join(collected[-15:])
        raise BuildError(f"{what} 失败（退出码 {proc.returncode}）：\n{tail}")
    return out


# ---------------------------------------------------------------- 版本 / 路径


def project_version() -> str:
    version_file = ROOT / "VERSION"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "0.1.0"


def find_runtime_archive() -> Path | None:
    """优先用 .build\\ 里预置的运行时压缩包（现场没网也能构建）。"""
    if not WORK.exists():
        return None
    for pattern in ("python-*-embed-amd64.zip", "cpython-*install_only.tar.gz"):
        hits = sorted(WORK.glob(pattern))
        if hits:
            return hits[-1]
    return None


# ---------------------------------------------------------------- 1. 运行时


def ensure_runtime(archive: Path | None) -> None:
    """检查（必要时准备）dist\\runtime 里的便携 Python。"""
    python = RUNTIME / "python.exe"
    if not IS_WINDOWS:
        # WSL 上没法运行 Windows python.exe，只校验文件是否就位
        log("非 Windows：跳过运行时校验（构建请在 Windows 上跑）", level="warn")
        return
    if python.exists() and (RUNTIME / "Lib" / "site-packages").exists():
        version = run([python, "-c", "import sys;print(sys.version.split()[0])"], what="运行时版本")
        log(f"运行时已就位：{version.strip()}")
        return

    source = archive or find_runtime_archive()
    if source is None:
        raise BuildError(
            "找不到运行时压缩包。请把 python-3.11.9-embed-amd64.zip 放到 .build\\ 下，"
            "或用 --runtime-archive <路径> 指定。"
        )
    log(f"从 {source.name} 解出运行时", level="step")
    extract_runtime(source)


def extract_runtime(archive: Path) -> None:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if any(n.startswith("python/") for n in names):
                # PBS 的 zip：内容是 python\ 目录
                zf.extractall(WORK / "rt_tmp")
                shutil.move(str(WORK / "rt_tmp" / "python"), str(RUNTIME))
                shutil.rmtree(WORK / "rt_tmp", ignore_errors=True)
            else:
                zf.extractall(RUNTIME)
    else:
        run(["tar.exe", "-xzf", archive, "-C", WORK / "rt_tmp"], what="解压运行时", cwd=ROOT)
        shutil.move(str(WORK / "rt_tmp" / "python"), str(RUNTIME))
        shutil.rmtree(WORK / "rt_tmp", ignore_errors=True)

    fix_pth()
    bootstrap_pip()


def fix_pth() -> None:
    """embeddable 默认不加载 site-packages —— 不补这个，pip 装的包全都 import 不到。"""
    pths = sorted(RUNTIME.glob("python3*._pth"))
    if not pths:
        log("没有 python3xx._pth（PBS 运行时自带 pip，跳过）")
        return
    pth = pths[0]
    # 第一行必须是 <名称>.zip（标准库打包在这里），写错的话 python.exe 连 sys 都起不来。
    # 反斜杠只写一个：写成 Lib\\site-packages 时 Python 找不到目录，pip 装的包全 import 不到。
    zip_entry = pth.stem + ".zip"
    lines = [zip_entry, ".", "Lib" + chr(92) + "site-packages", "", "import site"]
    with pth.open("w", encoding="ascii", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    log(f"已修正 {pth.name}：{zip_entry} + Lib\\site-packages + import site")


def _has_module(python: Path, module: str) -> bool:
    """静默探测：模块能不能 import（pip 有没有装）。"""
    try:
        proc = subprocess.run(
            [str(python), "-c", f"import {module}"], capture_output=True, timeout=120
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def bootstrap_pip() -> None:
    python = RUNTIME / "python.exe"
    if _has_module(python, "pip"):
        log("pip 已存在，跳过引导")
        return
    get_pip = WORK / "get-pip.py"
    if not get_pip.exists():
        raise BuildError(f"缺少 {get_pip}（embeddable 没有 pip，需要它引导）")
    run([python, get_pip, "--no-warn-script-location"], what="安装 pip", timeout=600)


# ---------------------------------------------------------------- 2. 依赖


def install_deps(wheels_dir: Path | None, pip_index: str) -> None:
    python = RUNTIME / "python.exe"
    req = ROOT / "requirements-win.txt"
    if not req.exists():
        raise BuildError(f"缺少 {req}")
    if not python.exists():
        raise BuildError("dist\\runtime\\python.exe 不存在，先跑不带 --skip-runtime 的构建。")

    cmd = [
        python,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--disable-pip-version-check",
        "-r",
        req,
    ]

    wheels = wheels_dir or (ROOT / "wheels")
    whl_count = len(list(wheels.glob("*.whl"))) if wheels.exists() else 0
    if whl_count:
        log(f"检测到离线 wheel（{whl_count} 个），全程不联网安装", level="step")
        cmd += ["--no-index", "--find-links", wheels]
    elif pip_index:
        log(f"联网安装（源：{pip_index}）", level="step")
        cmd += ["-i", pip_index]
    else:
        log("联网安装（默认 PyPI）", level="step")

    run(cmd, what="安装依赖", timeout=3600, stream=True)

    lock = DIST / "deps.lock"
    frozen = run([python, "-m", "pip", "freeze"], what="生成 deps.lock", timeout=180)
    lock.write_text(frozen, encoding="utf-8")
    log(f"依赖版本已锁定：deps.lock（{len(frozen.strip().splitlines())} 条）")


CORE_IMPORTS = (
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

OPTIONAL_IMPORTS = ("pywt", "statsmodels", "sklearn", "numba")


def verify_imports() -> None:
    """冒烟测试：核心依赖缺一个页面就起不来，必须在打包前中止。"""
    python = RUNTIME / "python.exe"
    if not python.exists():
        raise BuildError("dist\\runtime\\python.exe 不存在，无法验证依赖。")
    log("验证运行时依赖", level="step")
    missing = [name for name in CORE_IMPORTS if not _has_module(python, name)]
    if missing:
        raise BuildError(
            "运行时缺少核心依赖：" + ", ".join(missing) + "；请重跑依赖安装（不要加 --skip-deps）"
        )
    missing_optional = [name for name in OPTIONAL_IMPORTS if not _has_module(python, name)]
    if missing_optional:
        log(
            "缺少扩展算法依赖：" + ", ".join(missing_optional) + "（只影响对应算法）",
            level="warn",
        )
    else:
        log("核心依赖与扩展算法依赖全部可导入")


# ---------------------------------------------------------------- 3. 代码


def copy_code() -> None:
    log("拷程序代码", level="step")
    for name in ("app", "src", "tools", "configs", "launcher", ".streamlit"):
        source = ROOT / name
        if not source.exists():
            log(f"跳过（不存在）：{name}", level="warn")
            continue
        target = DIST / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        )

    # 用户手册只带 docs\manual\（对外文档）；docs\plans 之类内部文档不进安装包
    manual = ROOT / "docs" / "manual"
    if manual.is_dir():
        target = DIST / "docs"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(manual, target)
        log(f"用户手册已带上：{', '.join(p.name for p in manual.iterdir())}")
    else:
        log("没有 docs\\manual，安装包不带用户手册", level="warn")

    for name in ("VERSION", "run.py", "start.bat", "stop.bat", "requirements-win.txt", "build.bat"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, DIST / name)
    for folder in ("data", "logs", "backup"):
        (DIST / folder).mkdir(exist_ok=True)
    (DIST / "installer").mkdir(exist_ok=True)
    for cmd_file in (ROOT / "installer").glob("*.cmd"):
        shutil.copy2(cmd_file, DIST / "installer" / cmd_file.name)
    folders = sorted(p.name for p in DIST.iterdir() if p.is_dir())
    log(f"代码已就位：{', '.join(folders)}", level="info")


# ---------------------------------------------------------------- 4. NSSM


def get_nssm() -> None:
    target = RUNTIME / "nssm.exe"
    if target.exists():
        log("NSSM 已就位")
        return
    log("准备 NSSM（开机自启服务用）", level="step")
    zip_path = WORK / "nssm.zip"
    try:
        import urllib.request

        if not zip_path.exists():
            cached = sorted(WORK.glob("nssm*.zip"))
            if cached:
                zip_path = cached[-1]
                log(f"使用本地 NSSM：{zip_path.name}")
            else:
                urllib.request.urlretrieve("https://nssm.cc/release/nssm-2.24.zip", zip_path)  # noqa: S310
        with zipfile.ZipFile(zip_path) as zf:
            member = next((n for n in zf.namelist() if n.lower().endswith("win64/nssm.exe")), None)
            if member is None:
                raise BuildError("nssm.zip 里没有 win64\\nssm.exe")
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        log(f"NSSM 已就位：{target}")
    except Exception as exc:  # noqa: BLE001
        log(f"NSSM 没拿到（{exc}）—— 开机自启不可用，其余正常", level="warn")
        log(f"手动补：把 nssm.exe(win64) 放到 {target}", level="warn")


# ---------------------------------------------------------------- 5. 启动器


def build_launcher() -> None:
    log("编译启动器 LionMonitor.exe", level="step")
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe",
        Path(os.environ.get("WINDIR", r"C:\Windows"))
        / "Microsoft.NET/Framework/v4.0.30319/csc.exe",
    ]
    csc = next((c for c in candidates if c.exists()), None)
    if csc is None:
        raise BuildError("找不到 csc.exe（.NET Framework 4.x 没装？）")

    source = DIST / "launcher" / "LionMonitor.cs"
    if not source.exists():
        raise BuildError(f"缺少启动器源码：{source}")
    target = DIST / "LionMonitor.exe"
    cmd = [
        csc,
        "/nologo",
        "/target:winexe",
        "/optimize+",
        f"/out:{target}",
        "/reference:System.dll",
        "/reference:System.Windows.Forms.dll",
        "/reference:System.Drawing.dll",
    ]
    icon = ROOT / "installer" / "lion.ico"
    if icon.exists():
        cmd.append(f"/win32icon:{icon}")
        log(f"启动器图标：{icon.name}")
    else:
        # 缺图标不中止构建（还能用），但必须显式告警，别悄悄出个默认图标
        log(f"缺图标 {icon}，启动器会用默认 EXE 图标", level="warn")
    cmd.append(source)
    run(cmd, what="编译启动器", timeout=300)
    size_kb = target.stat().st_size // 1024
    log(f"启动器已生成：{target}（{size_kb} KB）")


# ---------------------------------------------------------------- 6. 自检


def run_selfcheck() -> int:
    log("跑构建产物自检", level="step")
    python = RUNTIME / "python.exe"
    script = DIST / "tools" / "selfcheck.py"
    if not (python.exists() and script.exists()):
        log("运行时或自检脚本不在，跳过自检", level="warn")
        return 0
    proc = subprocess.run(
        [str(python), str(script), "--project-root", str(DIST), "--port", "8501", "--no-log"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.rstrip().splitlines():
        log(f"   {line}")
    if proc.returncode != 0:
        log("自检有失败项（见上）—— Windows 上不该失败，请对照处理", level="warn")
    return proc.returncode


# ---------------------------------------------------------------- 7. 安装包


def copy_demo_data(source: Path) -> None:
    """把现场测试数据放进 ``dist\\demo_data\\四通道保存``，随演示安装包一起分发。

    为什么不放 ``dist\\data``：安装脚本要排除掉这份演示数据，改用带
    ``uninsneveruninstall`` 的条目单独装到 ``{app}\\data``（这样卸载不会删库）。
    Inno 的 ``Excludes`` 是按「路径里任意一段叫这个名字」匹配的 —— 用 ``data\\*``
    会把 plotly 里的 ``plotly\\graph_objs\\layout\\template\\data\\*`` 一起排掉，
    装完 streamlit 直接 ImportError、首页 500（2026-11-06 实测踩过），
    所以这里换成不会撞名的 ``demo_data``。
    """
    if not source.is_dir():
        raise BuildError(f"演示数据目录不存在：{source}")
    target = DIST / "demo_data" / "四通道保存"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    files = sum(1 for item in target.rglob("*") if item.is_file())

    # 演示库也放进 demo_data：安装时由 tools\seed_demo.py 拷到 data\lion.db。
    # 这样它不是「安装器记录过的文件」，卸载不会删 —— 用户导入的数据不会被顺手洗掉。
    demo_db = DIST / "data" / "lion.db"
    demo_copy = DIST / "demo_data" / "lion.db"
    if demo_db.is_file() and (
        not demo_copy.is_file() or demo_db.stat().st_size >= demo_copy.stat().st_size
    ):
        shutil.copy2(demo_db, demo_copy)
        for suffix in ("-wal", "-shm"):
            leftover = DIST / "data" / f"lion.db{suffix}"
            if leftover.exists():
                leftover.unlink()
    elif not demo_copy.is_file():
        log(
            "没有演示库可用（dist\\data\\lion.db 与 dist\\demo_data\\lion.db 都不存在）",
            level="warn",
        )
    log(f"演示数据已复制：{target}（{files} 个文件 + 演示库）")


def clean_build_artifacts(keep_data: bool = False) -> None:
    """清掉不该进包的东西；**``dist\\data`` 只允许留 ``lion.db``**。

    为什么 data\\ 必须清空：安装脚本的主条目是 ``Source: "..\\dist\\*"``，凡是躺在
    ``dist\\data`` 里的文件都会被安装器记上一笔，卸载时按记录删掉——2026-11-06 实测：
    卸载后 ``data\\四通道保存`` 的 13 个 .dat 全没了，只剩库文件，等于把用户的现场数据
    一起洗了。演示数据统一放 ``dist\\demo_data``，装完由 ``tools\\seed_demo.py`` 新建到
    ``data\\``，卸载器不认识这些文件，数据才留得住。

    ``keep_data=True``（演示版）只保留 ``dist\\data\\lion.db`` 给绿色包直接试看；
    安装包里它被 ``Excludes`` 排除，用户数据仍由 seed_demo 铺。
    """
    data_dir = DIST / "data"
    if data_dir.is_dir():
        for entry in list(data_dir.iterdir()):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                continue
            if keep_data and entry.name == "lion.db":
                continue
            try:
                entry.unlink(missing_ok=True)
            except OSError as exc:
                log(f"清理 {entry} 失败：{exc}", level="warn")
    for target in (DIST / "logs").glob("*"):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    # 自检 / 冒烟测试导入 lion_monitor 时会生成 __pycache__，不必进安装包
    for folder in ("app", "src", "tools"):
        for cache in (DIST / folder).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)


def _assert_dist_data_clean() -> None:
    """编译安装包前确认 ``dist\\data`` 只剩库文件。

    安装器 ``[Files]`` 只排除了 ``data\\lion.db*``；一旦 ``data\\`` 里还有别的东西
    （老版本复制过去的演示 .dat 就是这样），它们会被安装器记录、卸载时删掉，
    用户数据跟着没。这里宁可构建失败，也不要打一个「卸载会丢数据」的包。
    """
    data_dir = DIST / "data"
    if not data_dir.is_dir():
        return
    stray = [
        item
        for item in data_dir.rglob("*")
        if item.is_file() and not item.name.startswith("lion.db")
    ]
    if stray:
        names = "、".join(item.name for item in stray[:5])
        raise BuildError(
            f"dist\\data 里还有 {len(stray)} 个不该进包的文件（{names}…）："
            "安装器会记录它们并在卸载时删除。先跑 clean_build_artifacts() 或手动清空 dist\\data"
        )


def build_installer(inno_compiler: str) -> None:
    log("打安装包（Inno Setup）", level="step")
    iscc = Path(inno_compiler) if inno_compiler else None
    if iscc is None or not iscc.exists():
        guesses = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Inno Setup 6/ISCC.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6/ISCC.exe",
            # winget 默认按用户安装到这里
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Inno Setup 6/ISCC.exe",
        ]
        iscc = next((g for g in guesses if g.exists()), None)
    if iscc is None:
        log(
            "没找到 Inno Setup 6（ISCC.exe）—— dist\\ 已是可用的绿色包，只是没有安装程序",
            level="warn",
        )
        log("装上 Inno Setup 后重跑本脚本即可", level="warn")
        return
    iss = ROOT / "installer" / "LionMonitor.iss"
    _assert_dist_data_clean()
    run([iscc, f"/DMyAppVersion={project_version()}", iss], what="Inno Setup 编译", timeout=1800)
    setups = sorted((ROOT / "installer" / "output").glob("LionMonitor-Setup-*.exe"))
    if setups:
        setup = setups[-1]
        log(f"安装包：{setup}（{setup.stat().st_size // (1024 * 1024)} MB）", level="info")


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lion-Monitor Windows 构建")
    parser.add_argument("--skip-runtime", action="store_true", help="复用现有 dist\\runtime")
    parser.add_argument("--skip-deps", action="store_true", help="跳过依赖安装")
    parser.add_argument("--no-installer", action="store_true", help="只到 dist\\，不打安装包")
    parser.add_argument("--runtime-archive", default="", help="本地运行时压缩包路径")
    parser.add_argument("--wheels-dir", default="", help="离线 wheel 目录")
    parser.add_argument("--pip-index", default="https://pypi.tuna.tsinghua.edu.cn/simple")
    parser.add_argument("--inno-compiler", default="")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="保留 dist\\data 里的演示库（打自带数据的演示安装包）",
    )
    parser.add_argument(
        "--demo-data",
        default="",
        help="演示数据目录，复制到 dist\\demo_data\\四通道保存（隐含 --keep-data）",
    )
    args = parser.parse_args(argv)

    started = time.time()
    log_path = open_log()
    log(f"Lion-Monitor 构建开始（版本 {project_version()}）")
    log(f"日志：{log_path}")

    try:
        DIST.mkdir(exist_ok=True)
        if args.skip_runtime:
            log("跳过运行时准备（复用 dist\\runtime）", level="step")
        else:
            ensure_runtime(Path(args.runtime_archive) if args.runtime_archive else None)

        copy_code()

        if args.demo_data:
            copy_demo_data(Path(args.demo_data))
            # 绿色包也要能脱离开发机运行：把库里的绝对路径改到 dist\demo_data\四通道保存
            run(
                [RUNTIME / "python.exe", DIST / "tools" / "relocate_demo.py", DIST],
                what="重定位演示数据",
                timeout=120,
            )

        if args.skip_deps:
            log("跳过依赖安装", level="step")
        else:
            install_deps(Path(args.wheels_dir) if args.wheels_dir else None, args.pip_index)

        verify_imports()
        get_nssm()
        build_launcher()
        run_selfcheck()
        clean_build_artifacts(keep_data=bool(args.keep_data or args.demo_data))
        if not args.no_installer:
            build_installer(args.inno_compiler)

        elapsed = (time.time() - started) / 60
        log("")
        log(f"构建完成，用时 {elapsed:.1f} 分钟")
        log(f"  绿色包：{DIST}")
        log(f"  下一步：双击 {DIST / 'LionMonitor.exe'} 试启动")
        log(f"  构建日志：{log_path}")
        return 0
    except BuildError as exc:
        log("")
        log(f"构建失败：{exc}", level="error")
        log(f"完整日志：{log_path}", level="warn")
        return 1
    except Exception as exc:  # noqa: BLE001
        log("")
        log(f"构建异常：{type(exc).__name__}: {exc}", level="error")
        log(f"完整日志：{log_path}", level="warn")
        import traceback

        traceback.print_exc()
        if _log_handle:
            traceback.print_exc(file=_log_handle)
        return 1
    finally:
        close_log()


if __name__ == "__main__":
    sys.exit(main())
