@echo off
chcp 65001 >nul
rem 停止 Lion-Monitor：服务模式和启动器模式都能停。
rem 用法：双击，或命令行 stop.bat 8501（端口不是 8501 时）
setlocal
set PORT=%~1
if "%PORT%"=="" set PORT=8501

sc query LionMonitor >nul 2>&1
if not errorlevel 1 (
  sc stop LionMonitor >nul 2>&1
  echo 已停止服务模式（LionMonitor 服务）
)

set KILLED=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%p >nul 2>&1
  if not errorlevel 1 (
    echo 已结束占用 %PORT% 的进程 pid=%%p
    set KILLED=1
  )
)

if "%KILLED%"=="0" echo 没有发现正在运行的 Lion-Monitor（端口 %PORT% 空闲）
echo.
pause
