@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
if defined ENV_FILE_PATH for %%I in ("%ENV_FILE_PATH%") do set "ENV_FILE_PATH=%%~fI"
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not defined ENV_FILE_PATH set "ENV_FILE_PATH=%~dp0.env"
set "HOST=127.0.0.1"
set "PORT=8000"
rem 后台任务跟随网页“定时”页的“每日自动续火花”开关；不在这里强制关闭。
set "SPARKKEEPER_BACKGROUND_TASKS="
rem This local entry point uses its own .env rather than stale inherited secrets.
set "AUTH_TOKEN="
set "URL_ACCESS_TOKEN="
set "URL_PASSWORD_SALT="
set "URL_PASSWORD_HASH="
if not exist "%~dp0venv\Scripts\python.exe" (
  echo [错误] 未找到本项目的 venv\Scripts\python.exe。
  echo 请先双击同文件夹中的「安装依赖.bat」，安装完成后再启动。
  pause
  exit /b 1
)
"%~dp0venv\Scripts\python.exe" "%~dp0startup_check.py"
if errorlevel 1 (
  echo [停止] 运行环境未就绪，尚未启动服务或读取账号数据。
  pause
  exit /b 1
)
"%~dp0venv\Scripts\python.exe" "%~dp0password_setup.py"
if errorlevel 1 (
  echo [停止] 密码配置未完成，服务尚未启动。
  pause
  exit /b 1
)
"%~dp0venv\Scripts\python.exe" "%~dp0tools\version_info.py"
echo [本机地址] http://127.0.0.1:8000/
echo [运行模式] 每日自动续火花跟随网页“定时”页开关；关闭时只手动操作。
echo [停止服务] 按 Ctrl+C，运行期间保留此窗口。
"%~dp0venv\Scripts\python.exe" "%~dp0app.py"
set "SPARKKEEPER_EXIT=%ERRORLEVEL%"
echo.
echo [服务已退出] 退出码：%SPARKKEEPER_EXIT%
pause
exit /b %SPARKKEEPER_EXIT%
