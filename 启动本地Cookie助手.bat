@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONUTF8=1"
chcp 65001 >nul
set "HELPER_PYTHON=%~dp0venv\Scripts\python.exe"
if not exist "%HELPER_PYTHON%" (
  echo 未找到本项目的 venv\Scripts\python.exe。
  echo 请先准备源码运行环境，然后重新启动本机助手。
  pause
  exit /b 1
)
"%HELPER_PYTHON%" "%~dp0desktop\local_cookie_helper.py" %*
if errorlevel 1 pause
