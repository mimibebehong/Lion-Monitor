<#
.SYNOPSIS
    构建 Lion-Monitor Windows 安装包（方案 A：便携 Python + 启动器 + Inno Setup）。

.DESCRIPTION
    在 Windows 构建机上（Win10 / Win11 / Server 2022 都可以）运行，产出：
        dist\                       可直接拷贝运行的绿色目录
        installer\output\LionMonitor-Setup-<版本>.exe

    需要：PowerShell 5.1、能联网（下载运行时和依赖）、Inno Setup 6（可选，只装包时才要）。

.EXAMPLE
    # 完整构建（下载运行时 + 装依赖 + 编启动器 + 打安装包）
    .\scripts\build_win.ps1

.EXAMPLE
    # 只更新程序代码（运行时和依赖已就绪，最快，约 10 秒）
    .\scripts\build_win.ps1 -SkipRuntime -SkipDeps

.EXAMPLE
    # 只到 dist，不打安装包（调试用）
    .\scripts\build_win.ps1 -NoInstaller
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.11",
    # embeddable 回退用（GitHub 不通时走它；项目不用 tkinter，embeddable 够用）
    [string]$EmbeddableVersion = "3.11.9",
    [string]$PbsTag = "",
    # 本地已下载的运行时压缩包（PBS 的 .tar.gz 或 embeddable 的 .zip 都行）
    [string]$RuntimeArchive = "",
    # pip 源；现场直连 pypi.org 慢时改国内镜像
    [string]$PipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple",
    # 离线 wheel 目录：存在且非空就完全不联网装依赖（构建机没网也能构建）
    [string]$WheelsDir = "",
    [switch]$SkipRuntime,
    [switch]$SkipDeps,
    [switch]$NoInstaller,
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dist = Join-Path $Root "dist"
$Runtime = Join-Path $Dist "runtime"
$Work = Join-Path $Root ".build"

# 全程落盘：闪退、双击运行、远程排障都能查（2026-11-05「报错都要落 log」）
if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work -Force | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BuildLog = Join-Path $Work ("build-" + $Stamp + ".log")
$script:TranscriptOn = $false
try {
    Start-Transcript -Path $BuildLog -Force | Out-Null
    $script:TranscriptOn = $true
} catch {
    Write-Warning "无法写构建日志（$BuildLog）：$($_.Exception.Message)"
}

function Write-Step($Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Assert-LastExit($What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（退出码 $LASTEXITCODE），后面步骤会用到它的产物，已停止。"
    }
}

function Get-ProjectVersion {
    $versionFile = Join-Path $Root "VERSION"
    if (Test-Path $versionFile) { return (Get-Content $versionFile -Raw).Trim() }
    return "0.1.0"
}

# ---------------------------------------------------------------- 1. 便携运行时

function Install-Runtime {
    Write-Step "准备便携 Python 运行时"

    if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work | Out-Null }

    if ($RuntimeArchive) {
        if (-not (Test-Path $RuntimeArchive)) { throw "指定的运行时压缩包不存在：$RuntimeArchive" }
        Expand-RuntimeArchive -Archive (Resolve-Path $RuntimeArchive).Path -IsEmbeddable:$false
        return
    }

    # 预置在 .build\ 里的运行时压缩包（构建机没网时靠它）
    $cached = Get-ChildItem -Path $Work -Filter "python-*-embed-amd64.zip" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($cached) {
        Write-Host "使用预置运行时：$($cached.Name)"
        Expand-RuntimeArchive -Archive $cached.FullName -IsEmbeddable
        Install-EmbeddablePip
        return
    }

    # 1) 优先 python-build-standalone（自带 pip + 完整标准库）
    try {
        Install-PbsRuntime
        return
    } catch {
        Write-Warning "python-build-standalone 拿不到（$($_.Exception.Message)）"
        Write-Warning "改走官方 embeddable 运行时（项目不用 tkinter，功能不受影响）"
    }

    # 2) 回退官方 embeddable（python.org / npmmirror）
    Install-EmbeddableRuntime
}

function Expand-RuntimeArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [switch]$IsEmbeddable
    )
    Write-Step "解压运行时到 dist\runtime"
    if (Test-Path $Runtime) { Remove-Item $Runtime -Recurse -Force }
    $extract = Join-Path $Work "runtime_extract"
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
    New-Item -ItemType Directory -Path $extract | Out-Null

    if ($Archive -like "*.zip") {
        Expand-Archive -Path $Archive -DestinationPath $extract -Force
        # embeddable 的 zip 是平铺的；PBS 的 zip 里面是 python\ 目录
        $inner = Join-Path $extract "python"
        if (Test-Path $inner) { Move-Item -Path $inner -Destination $Runtime }
        else { Move-Item -Path $extract -Destination $Runtime }
    } else {
        if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
            throw "系统没有 tar.exe（Windows 10 1803 以下？），请解压 $Archive 后手动放到 $Runtime"
        }
        & tar.exe -xzf $Archive -C $extract
        Assert-LastExit "解压运行时"
        $inner = Join-Path $extract "python"
        if (-not (Test-Path $inner)) { throw "运行时压缩包结构不对：没有 python\ 目录" }
        Move-Item -Path $inner -Destination $Runtime
    }
    if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }

    $exe = Join-Path $Runtime "python.exe"
    if (-not (Test-Path $exe)) { throw "解压后没有 python.exe，包可能损坏。" }
    & $exe -c "import sys; print('运行时版本', sys.version)"
    Assert-LastExit "运行时自检"
}

function Install-PbsRuntime {
    $tag = $PbsTag
    if (-not $tag) {
        Write-Host "查询 python-build-standalone 最新版本…"
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" `
            -UseBasicParsing -Headers @{ "User-Agent" = "lion-monitor-build" } -TimeoutSec 30
        $tag = $release.tag_name
    }
    Write-Host "运行时版本：$tag"

    $pattern = "cpython-$PythonVersion\.[0-9]+\+.*-x86_64-pc-windows-msvc-install_only\.tar\.gz$"
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/$tag" `
        -UseBasicParsing -Headers @{ "User-Agent" = "lion-monitor-build" } -TimeoutSec 30
    $asset = $release.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if (-not $asset) { throw "在 $tag 里找不到 Python $PythonVersion 的 Windows install_only 包" }

    $archive = Join-Path $Work $asset.name
    if (-not (Test-Path $archive)) {
        Write-Host "下载 $($asset.name)（约 50 MB）…"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -UseBasicParsing -TimeoutSec 900
    }
    Expand-RuntimeArchive -Archive $archive
}

function Install-EmbeddableRuntime {
    $archive = Join-Path $Work ("python-{0}-embed-amd64.zip" -f $EmbeddableVersion)
    if (-not (Test-Path $archive)) {
        # 先试国内镜像（快），再试官方
        $urls = @(
            "https://registry.npmmirror.com/-/binary/python/$EmbeddableVersion/python-$EmbeddableVersion-embed-amd64.zip",
            "https://www.python.org/ftp/python/$EmbeddableVersion/python-$EmbeddableVersion-embed-amd64.zip"
        )
        $ok = $false
        foreach ($url in $urls) {
            try {
                Write-Host "下载 $url …"
                Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing -TimeoutSec 600
                if ((Get-Item $archive).Length -gt 5MB) { $ok = $true; break }
            } catch {
                Write-Warning "下载失败：$($_.Exception.Message)"
            }
        }
        if (-not $ok) {
            throw "embeddable 运行时也下载失败。请手动下载 python-$EmbeddableVersion-embed-amd64.zip，然后 -RuntimeArchive <路径> 重跑。"
        }
    }

    Expand-RuntimeArchive -Archive $archive -IsEmbeddable

    # embeddable 默认不加载 site-packages，也没有 pip —— 两个都得补
    $pth = Get-ChildItem -Path $Runtime -Filter "python3*._pth" | Select-Object -First 1
    if (-not $pth) { throw "embeddable 包里找不到 python3xx._pth，包可能损坏。" }
    $lines = @($pth.Name, ".", "Lib\site-packages", "", "import site") -join "`r`n"
    Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII
    Write-Host "已修正 $($pth.Name)：加入 Lib\site-packages + import site"

    Install-EmbeddablePip
}

function Install-EmbeddablePip {
    $python = Join-Path $Runtime "python.exe"
    $getPip = Join-Path $Work "get-pip.py"
    if (-not (Test-Path $getPip)) {
        Write-Host "下载 get-pip.py …"
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing -TimeoutSec 120
    }
    & $python $getPip --no-warn-script-location
    Assert-LastExit "安装 pip"
    Write-Host "pip 已就位（embeddable 没有 pip.exe，用 python.exe -m pip 调用）"
}

# ---------------------------------------------------------------- 2. 依赖

function Install-Deps {
    Write-Step "安装依赖到运行时（约 500 MB，首次 5-15 分钟）"
    $python = Join-Path $Runtime "python.exe"
    $req = Join-Path $Root "requirements-win.txt"
    if (-not (Test-Path $python)) { throw "没有 runtime\python.exe，先跑不带 -SkipRuntime 的构建。" }

    $pipArgs = @("-m", "pip", "install", "--no-cache-dir", "--only-binary=:all:",
                 "--disable-pip-version-check", "-r", $req)

    $wheels = $WheelsDir
    if (-not $wheels) { $wheels = Join-Path $Root "wheels" }
    $offline = (Test-Path $wheels) -and ((Get-ChildItem -Path $wheels -Filter "*.whl" -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)
    if ($offline) {
        Write-Host "检测到离线 wheel 包（$((Get-ChildItem -Path $wheels -Filter '*.whl').Count) 个），全程不联网安装"
        $pipArgs += @("--no-index", "--find-links", $wheels)
    } elseif ($PipIndex) {
        $pipArgs += @("-i", $PipIndex)
    }
    & $python @pipArgs
    Assert-LastExit "安装依赖"

    Write-Step "锁定依赖版本到 deps.lock（防现场漂移）"
    $lock = Join-Path $Dist "deps.lock"
    & $python -m pip freeze | Out-File -FilePath $lock -Encoding utf8
    Assert-LastExit "生成 deps.lock"
    Write-Host "共 $((Get-Content $lock).Count) 行锁定记录：$lock"
}

# ---------------------------------------------------------------- 3. 拷代码

function Copy-AppCode {
    Write-Step "拷程序代码（app / src / tools / configs）"
    $pairs = @(
        @{ From = "app"; To = "app" },
        @{ From = "src"; To = "src" },
        @{ From = "tools"; To = "tools" },
        @{ From = "configs"; To = "configs" }
    )
    foreach ($pair in $pairs) {
        $from = Join-Path $Root $pair.From
        $to = Join-Path $Dist $pair.To
        if (-not (Test-Path $from)) { Write-Host "跳过（不存在）：$from" -ForegroundColor Yellow; continue }
        if (Test-Path $to) { Remove-Item $to -Recurse -Force }
        Copy-Item -Path $from -Destination $to -Recurse -Force
    }

    # 清掉开发垃圾，别打进安装包
    Get-ChildItem -Path $Dist -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force
    Get-ChildItem -Path $Dist -Recurse -File -Include "*.pyc", "*.pyo" -ErrorAction SilentlyContinue |
        Remove-Item -Force

    # 启动器源码留着（现场排障能看），编译产物单独放
    $launcherSrc = Join-Path $Root "launcher"
    if (Test-Path $launcherSrc) {
        $to = Join-Path $Dist "launcher"
        if (Test-Path $to) { Remove-Item $to -Recurse -Force }
        Copy-Item -Path $launcherSrc -Destination $to -Recurse -Force
    }

    foreach ($extra in @("VERSION", "start.bat", "stop.bat", "requirements-win.txt", "deps.lock")) {
        $from = Join-Path $Root $extra
        if (Test-Path $from) { Copy-Item -Path $from -Destination (Join-Path $Dist $extra) -Force }
    }

    # 空目录预建（Inno 也会建，这里保证绿色包拷走就能用）
    foreach ($dir in @("data", "logs", "backup")) {
        $path = Join-Path $Dist $dir
        if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
    }
}

# ---------------------------------------------------------------- 4. 启动器

function Build-Launcher {
    Write-Step "编译启动器 LionMonitor.exe（Windows 自带 csc.exe）"
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path $csc)) {
        $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe"
    }
    if (-not (Test-Path $csc)) { throw "找不到 csc.exe（.NET Framework 4.x 未安装？）" }

    $src = Join-Path $Root "launcher\LionMonitor.cs"
    $out = Join-Path $Dist "LionMonitor.exe"
    $icon = Join-Path $Root "installer\lion.ico"
    $iconArg = @()
    if (Test-Path $icon) { $iconArg = @("/win32icon:$icon") }

    & $csc /nologo /target:winexe /optimize+ "/out:$out" `
        /reference:System.dll /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
        @iconArg $src
    Assert-LastExit "编译启动器"
    Write-Host "启动器已生成：$out（$([math]::Round((Get-Item $out).Length / 1KB)) KB）"
}

# ---------------------------------------------------------------- 5. NSSM

function Get-Nssm {
    Write-Step "准备 NSSM（把 Python 注册成 Windows 服务）"
    $target = Join-Path $Runtime "nssm.exe"
    if (Test-Path $target) { Write-Host "已存在：$target"; return }

    if (-not (Test-Path $Work)) { New-Item -ItemType Directory -Path $Work | Out-Null }
    $zip = Join-Path $Work "nssm.zip"
    $url = "https://nssm.cc/release/nssm-2.24.zip"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $Work -Force
        $candidate = Get-ChildItem -Path $Work -Recurse -Filter "nssm.exe" |
            Where-Object { $_.FullName -match "win64" } | Select-Object -First 1
        if (-not $candidate) { throw "压缩包里没有 win64\nssm.exe" }
        Copy-Item -Path $candidate.FullName -Destination $target -Force
        Write-Host "NSSM 已就位：$target"
    } catch {
        Write-Warning "NSSM 下载失败（$($_.Exception.Message)）。开机自启功能会不可用，其余功能正常。"
        Write-Warning "手动补：把 nssm.exe(win64) 放到 $target"
    }
}

# ---------------------------------------------------------------- 6. 打包

function Build-Installer {
    Write-Step "打安装包（Inno Setup）"
    $iscc = $InnoCompiler
    if (-not $iscc) {
        $guess = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        $iscc = $guess
    }
    if (-not $iscc -or -not (Test-Path $iscc)) {
        Write-Warning "没找到 Inno Setup 6（ISCC.exe）。dist\ 已经是可用的绿色包，只是没有安装程序。"
        Write-Warning "装上 Inno Setup 后再跑一次，或手动指定：-InnoCompiler 'C:\path\ISCC.exe'"
        return
    }

    $iss = Join-Path $Root "installer\LionMonitor.iss"
    Push-Location (Join-Path $Root "installer")
    try {
        & $iscc "/DMyAppVersion=$(Get-ProjectVersion)" $iss
        Assert-LastExit "Inno Setup 编译"
    } finally {
        Pop-Location
    }
    $setup = Get-ChildItem -Path (Join-Path $Root "installer\output") -Filter "LionMonitor-Setup-*.exe" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($setup) {
        Write-Host ""
        Write-Host "安装包：$($setup.FullName)（$([math]::Round($setup.Length / 1MB)) MB）" -ForegroundColor Green
    }
}

# ---------------------------------------------------------------- 主流程

$started = Get-Date
try {
Write-Host "Lion-Monitor 构建开始（版本 $(Get-ProjectVersion)，PowerShell $($PSVersionTable.PSVersion)）" -ForegroundColor Green

if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist | Out-Null }

if ($SkipRuntime) {
    Write-Step "跳过运行时下载（用现有 dist\runtime）"
    if (-not (Test-Path (Join-Path $Runtime "python.exe"))) {
        throw "dist\runtime\python.exe 不存在，不能跳过。去掉 -SkipRuntime 重跑。"
    }
} else {
    Install-Runtime
}

Copy-AppCode

if ($SkipDeps) {
    Write-Step "跳过依赖安装（用现有 dist\runtime）"
} else {
    Install-Deps
}

Get-Nssm
Build-Launcher

Write-Step "跑一遍自检（构建产物自检，失败会列出原因）"
$python = Join-Path $Runtime "python.exe"
& $python (Join-Path $Dist "tools\selfcheck.py") --project-root $Dist --port 8501 --json |
    Out-File -FilePath (Join-Path $Work "selfcheck.json") -Encoding utf8
$selfcheckCode = $LASTEXITCODE
Get-Content (Join-Path $Work "selfcheck.json") -Raw | Write-Host
if ($selfcheckCode -ne 0) {
    Write-Warning "构建产物自检有失败项（见上）。Windows 上浏览器/防火墙项在这里为 skip 属正常，其他失败项要修。"
}

if (-not $NoInstaller) {
    Build-Installer
}

$elapsed = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host ""
Write-Host "构建完成，用时 $elapsed 分钟。" -ForegroundColor Green
Write-Host "  绿色包：$Dist"
Write-Host "  下一步：dist\LionMonitor.exe 双击试启动；确认没问题再分发安装包。"
Write-Host "  构建日志：$BuildLog"
if ($script:TranscriptOn) { try { Stop-Transcript | Out-Null } catch { } }

} catch {
    Write-Host ""
    Write-Host "构建失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "出错的步骤和完整输出都在日志里：" -ForegroundColor Yellow
    Write-Host "  $BuildLog"
    if (Test-Path $BuildLog) {
        Write-Host ""
        Write-Host "最后 25 行：" -ForegroundColor Yellow
        Get-Content $BuildLog -Tail 25 | Write-Host
    }
    if ($script:TranscriptOn) { try { Stop-Transcript | Out-Null } catch { } }
    exit 1
}
