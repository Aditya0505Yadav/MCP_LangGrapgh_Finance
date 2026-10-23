@echo off
REM Double-click to launch the Finance Analysis app (MCP server + web UI).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launch.py
) else (
    echo .venv not found. Create it first:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    python launch.py
)
pause
