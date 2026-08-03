@echo off
rem Prior-Auth Checklist Agent - one-click launcher (Windows)
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed on this computer.
  echo 1. Download it from https://www.python.org/downloads/
  echo 2. IMPORTANT: tick "Add python.exe to PATH" on the first install screen.
  echo 3. Run this file again.
  echo.
  pause
  exit /b 1
)

python -c "import flask, anthropic, openai" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages ^(one-time, ~30 seconds^)...
  python -m pip install --quiet flask anthropic openai
)

echo Starting the Prior-Auth Agent... your browser will open automatically.
echo Keep this window open while you use the app. Press Ctrl+C to stop.
python app.py
pause
