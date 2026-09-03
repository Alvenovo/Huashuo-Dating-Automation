@echo off
cd /d "%~dp0"
echo Launch smoke P0-03/04. Does not reinstall. Closes and restarts ASUS Hall 3 times.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pytest tests/launch -m "launch" -v
) else (
  python -m pytest tests/launch -m "launch" -v
)
