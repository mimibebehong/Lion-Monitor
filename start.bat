@echo off
chcp 65001 >nul
rem 排障用：带控制台启动，能看到完整报错（普通用户用 LionMonitor.exe 双击即可）
cd /d "%~dp0"
set LION_LOG_DIR=%~dp0logs
set LION_DB=%~dp0data\lion.db
set PYTHONIOENCODING=utf-8
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo ============================================================
echo  Lion-Monitor 控制台模式（排障用）
echo  启动后浏览器访问 http://localhost:8501
echo  日志目录：%LION_LOG_DIR%
echo  关闭本窗口或按 Ctrl+C 即停止
echo ============================================================
"%~dp0runtime\python.exe" "%~dp0run.py" run "%~dp0app\main.py" --server.address 0.0.0.0 --server.port 8501 --server.headless true --server.runOnSave false --browser.gatherUsageStats false
echo.
echo 进程已退出，退出码 %ERRORLEVEL%
pause
