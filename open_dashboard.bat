@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Friday is not installed yet. Double-click install.bat first.
  pause
  exit /b 1
)
call "%~dp0scripts\ensure_deps.bat" || exit /b 1
".venv\Scripts\python.exe" -m friday --open-dashboard
if errorlevel 1 pause
