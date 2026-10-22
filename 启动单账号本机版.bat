@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "ENV_FILE_PATH=%~dp0.env"
set "HOST=127.0.0.1"
set "PORT=8000"
set "SPARKKEEPER_BACKGROUND_TASKS=0"
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
echo [版本] 单账号控制台 2.8（本机无密码门）
echo [本机地址] http://127.0.0.1:8000/
echo [运行模式] 后台定时任务暂停，可查看面板和手动操作。
echo [停止服务] 按 Ctrl+C，运行期间保留此窗口。
"%~dp0venv\Scripts\python.exe" "%~dp0app.py"
set "SPARKKEEPER_EXIT=%ERRORLEVEL%"
echo.
echo [服务已退出] 退出码：%SPARKKEEPER_EXIT%
pause
exit /b %SPARKKEEPER_EXIT%
