@echo off
title Jarvis - switch AI model
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\switch_model.ps1" %*
echo.
pause
