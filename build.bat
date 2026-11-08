@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Lion-Monitor 一键构建（不依赖 PowerShell —— 现场那台 Server 2022 上
rem powershell.exe 返回 0 却什么都不执行，所以构建逻辑全在 build.py 里）
rem
rem 用法：双击本文件；或 build.bat -skip-runtime -skip-deps   （只换代码，最快）

set "PY=dist\runtime\python.exe"

if exist "%PY%" goto havepython

echo [1/3] 解压便携 Python 运行时...
if not exist "dist\runtime" mkdir "dist\runtime"
if not exist ".build\python-3.11.9-embed-amd64.zip" (
  echo   缺少 .build\python-3.11.9-embed-amd64.zip，无法继续
  goto fail
)
tar.exe -xf ".build\python-3.11.9-embed-amd64.zip" -C "dist\runtime"
if errorlevel 1 (
  echo   解压失败
  goto fail
)

echo [2/3] 修正 python311._pth（让 pip 装的包能被导入）...
if not exist "dist\runtime\python311._pth" (
  echo   找不到 dist\runtime\python311._pth，运行时包可能不对
  goto fail
)
>  "dist\runtime\python311._pth" echo python311.zip
>> "dist\runtime\python311._pth" echo .
>> "dist\runtime\python311._pth" echo Lib\site-packages
>> "dist\runtime\python311._pth" echo.
>> "dist\runtime\python311._pth" echo import site

echo [3/3] 引导 pip...
if not exist ".build\get-pip.py" (
  echo   缺少 .build\get-pip.py
  goto fail
)
"%PY%" ".build\get-pip.py" --no-warn-script-location
if errorlevel 1 (
  echo   pip 引导失败
  goto fail
)

:havepython
echo.
echo ============================================================
echo  开始构建（详细过程同时写入 .build\build-*.log）
echo ============================================================
echo.
"%PY%" "build.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto failrc
echo ------------------------------------------------------------
echo  构建成功
echo  绿色包：%~dp0dist
echo  试运行：双击 dist\LionMonitor.exe
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:failrc
echo ------------------------------------------------------------
echo  构建失败（退出码 %RC%）
echo  日志目录：%~dp0.build
echo ------------------------------------------------------------
echo.
pause
exit /b %RC%

:fail
echo.
echo 构建中止。
echo.
pause
exit /b 1
