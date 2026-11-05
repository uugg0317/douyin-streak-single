@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1" -Action Remove
if errorlevel 1 (echo [失败] 未能取消开机自启。& pause & exit /b 1)
echo [完成] 已取消开机自启，不影响已保存的数据。
pause
