@echo off
chcp 65001 >nul
rem 一键收集诊断日志：把 logs 里所有日志合成一个 txt，放到桌面（找不到桌面就放安装目录）
rem 用法：  installer\collect_logs.cmd "D:\LionMonitor"
setlocal
set APPDIR=%~1
if "%APPDIR%"=="" set APPDIR=%~dp0..
if not exist "%APPDIR%\runtime\python.exe" (
  echo 找不到 %APPDIR%\runtime\python.exe，请在安装目录下运行。
  pause
  exit /b 1
)
"%APPDIR%\runtime\python.exe" "%APPDIR%\tools\collect_logs.py" --root "%APPDIR%"
echo.
echo 把这个 txt 文件发给技术支持即可。
pause
exit /b 0
