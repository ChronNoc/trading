@echo off
setlocal
cd /d "%~dp0"

set "REPO=%~dp0"
if not exist "%REPO%tools\start_assistant.py" (
  echo MNQ Assistant could not locate tools\start_assistant.py.
  echo Start this file from the mnq_bot repository root.
  pause
  exit /b 2
)

set "PYTHON=%REPO%.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo MNQ Assistant needs its local Python environment first.
  echo Missing: %PYTHON%
  echo Create it, then install dependencies into .venv before starting.
  pause
  exit /b 2
)

echo Starting MNQ Assistant in DELAYED PAPER mode.
echo No live or demo order execution is started by this launcher.
echo Bookmap free delayed-data mode: records and causally simulates paper trades only.
"%PYTHON%" -m tools.start_assistant --delayed-data-minutes 15 %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo MNQ Assistant stopped with error code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
