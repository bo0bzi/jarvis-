@echo off
title Friday - local AI setup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_local_ai.ps1"
echo.
pause
