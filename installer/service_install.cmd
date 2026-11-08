@echo off
chcp 65001 >nul
rem Lion-Monitor：注册为 Windows 服务（NSSM），实现「开机自启、不用登录」。
rem 由安装包调用：  service_install.cmd "C:\LionMonitor" 8501
rem 手动排障可单独运行：  installer\service_install.cmd "C:\LionMonitor" 8501
setlocal
set APPDIR=%~1
set PORT=%~2
if "%APPDIR%"=="" set APPDIR=%~dp0..
if "%PORT%"=="" set PORT=8501
set NSSM=%APPDIR%\runtime\nssm.exe
set LOG=%APPDIR%\logs\service.log

if not exist "%NSSM%" (
  echo [%DATE% %TIME%] ERROR 找不到 %NSSM%，无法注册服务 >> "%LOG%"
  echo 找不到 %NSSM%
  exit /b 1
)

rem 不是管理员就别往下走：nssm install 需要提权，会弹 UAC 对话框把静默安装卡死
rem （2026-11-06 实测：per-user 安装时 /VERYSILENT 停在这一步等人工确认）
net session >nul 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] SKIP 当前不是管理员，跳过服务注册 >> "%LOG%"
  echo 需要管理员权限才能注册开机自启服务：请用管理员身份重新运行安装包，或手动执行
  echo   %APPDIR%\installer\service_install.cmd "%APPDIR%" %PORT%
  exit /b 2
)

rem 先卸掉旧服务，避免升级时残留
"%NSSM%" stop LionMonitor >nul 2>&1
"%NSSM%" remove LionMonitor confirm >nul 2>&1

"%NSSM%" install LionMonitor "%APPDIR%\runtime\python.exe" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] ERROR 注册服务失败 >> "%LOG%"
  exit /b 1
)
rem AppDirectory 已设成安装目录，这里用相对路径 app\main.py，避免安装路径带空格时
rem 参数里的引号被 cmd/NSSM 吃掉导致服务起不来。
"%NSSM%" set LionMonitor AppParameters "-m streamlit run app\main.py --server.address 0.0.0.0 --server.port %PORT% --server.headless true --server.runOnSave false --browser.gatherUsageStats false" >> "%LOG%" 2>&1

"%NSSM%" set LionMonitor AppDirectory "%APPDIR%" >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor DisplayName "Lion-Monitor 锂电产线振动监测" >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor Description "开机自启的 Lion-Monitor 网页服务，局域网内用浏览器访问 http://本机IP:%PORT%" >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor Start SERVICE_AUTO_START >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor AppStdout "%APPDIR%\logs\server.log" >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor AppStderr "%APPDIR%\logs\server.log" >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor AppRotateFiles 1 >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor AppRotateBytes 10485760 >> "%LOG%" 2>&1
rem 日志/数据库默认就在 AppDirectory 下，不设 LION_LOG_DIR / LION_DB，
rem 避免安装路径带空格时环境变量被拆开。只设编码。
"%NSSM%" set LionMonitor AppEnvironmentExtra "PYTHONIOENCODING=utf-8" >> "%LOG%" 2>&1
rem 崩溃自动重启：滞后 5 秒，最多重试到服务被手动停止
"%NSSM%" set LionMonitor AppExit Default Restart >> "%LOG%" 2>&1
"%NSSM%" set LionMonitor AppRestartDelay 5000 >> "%LOG%" 2>&1

"%NSSM%" start LionMonitor >> "%LOG%" 2>&1
echo [%DATE% %TIME%] 服务注册并启动完成 port=%PORT% >> "%LOG%"
exit /b 0
