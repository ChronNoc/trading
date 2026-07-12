@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Self-check could not start: missing .venv\Scripts\python.exe
    exit /b 2
)

".venv\Scripts\python.exe" -m tools.self_check %*
exit /b %ERRORLEVEL%
