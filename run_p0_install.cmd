@echo off
cd /d "%~dp0"
echo Run this from an Administrator prompt. It uninstalls and reinstalls ASUS Hall.
set HALL_ALLOW_INSTALL=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pytest tests/install -m "install" -v
) else (
  python -m pytest tests/install -m "install" -v
)
