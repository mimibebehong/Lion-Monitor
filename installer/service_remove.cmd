@echo off
chcp 65001 >nul
rem Lion-Monitor：停止并注销 Windows 服务。卸载前跑一次，否则 runtime 里的 dll 被占用删不掉。
rem 由安装包卸载流程调用；也可手动执行。
rem
rem 只用系统自带 sc，**不调 nssm.exe**：NSSM 的清单是 requireAdministrator，
rem 非管理员调用会弹 UAC 对话框——静默卸载会卡在「nssm.exe 正在请求你的许可」
rem （2026-11-06 实测）。sc stop/delete 对 NSSM 注册的服务同样有效。
setlocal
set APPDIR=%~dp0..
set LOG=%APPDIR%\logs\service.log

sc stop LionMonitor >nul 2>&1
sc delete LionMonitor >nul 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] 服务未注销（可能未安装服务，或当前不是管理员）>> "%LOG%"
) else (
  echo [%DATE% %TIME%] 服务已停止并注销 >> "%LOG%"
)
exit /b 0
