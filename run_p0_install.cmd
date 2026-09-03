@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 请用「以管理员身份运行」的命令提示符执行本脚本。
echo 本操作会卸载并重装华硕大厅。
set HALL_ALLOW_INSTALL=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pytest tests/install -m "install" -v
) else (
  python -m pytest tests/install -m "install" -v
)
