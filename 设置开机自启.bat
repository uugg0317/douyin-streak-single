@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
echo 将在你登录 Windows 时自动启动续火花控制台（最小化窗口，仅本机 127.0.0.1:8000）。
echo 需要先在网页“定时”页打开“每日自动续火花”，启动后才会每天自动发送。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1" -Action Install
if errorlevel 1 (echo [失败] 未能设置开机自启。& pause & exit /b 1)
echo [完成] 已设置。取消请双击「取消开机自启.bat」。
pause
