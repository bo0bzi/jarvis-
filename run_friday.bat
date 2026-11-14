@echo off
title Jarvis - AI assistant
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Friday is not installed yet. Double-click install.bat first.
  pause
  exit /b 1
)
call "%~dp0scripts\ensure_deps.bat" || exit /b 1
rem Safety net: if Jarvis changed its own code and the new version never came up healthy, put the old one back first.
".venv\Scripts\python.exe" scripts\selfmod_guard.py
".venv\Scripts\python.exe" -m friday %*
if errorlevel 1 pause
