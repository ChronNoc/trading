@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo MNQ Prototype could not start: missing .venv\Scripts\python.exe
    echo Create the virtual environment and install the project dependencies first.
    pause
    exit /b 2
)

echo Starting MNQ Prototype in SHADOW mode...
echo PROTOTYPE DATA - NOT REAL MARKET DATA
".venv\Scripts\python.exe" "tools\start_prototype.py"
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
    echo.
    echo MNQ Prototype exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
