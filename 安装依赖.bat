@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONHOME="
set "PYTHONPATH="
echo [说明] 本步骤需要联网下载 Python 依赖与 Chromium；仅安装，不启动服务。
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
py -3 "%~dp0tools\ensure_venv.py" --project-root "%~dp0."
if errorlevel 1 goto failed
goto install_packages
:try_python
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [错误] 需要 Python 3.10 及以上。请先安装官方 Python，并启用 Python Launcher 或加入 PATH。
  goto failed
)
python "%~dp0tools\ensure_venv.py" --project-root "%~dp0."
if errorlevel 1 goto failed
:install_packages
"%~dp0venv\Scripts\python.exe" -m pip install --only-binary=greenlet,pydantic-core -r "%~dp0requirements.lock"
if errorlevel 1 goto failed
"%~dp0venv\Scripts\python.exe" "%~dp0startup_check.py" --dependencies-only
if errorlevel 1 goto failed
"%~dp0venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
"%~dp0venv\Scripts\python.exe" "%~dp0startup_check.py"
if errorlevel 1 goto failed
echo.
echo [完成] 依赖已安装。请双击「启动单账号本机版.bat」，直接打开本机控制台，无需设置管理密码。
pause
exit /b 0
:failed
echo.
echo [失败] 安装未完成。请查看上方错误并检查 Python、网络或目录写入权限。
pause
exit /b 1
