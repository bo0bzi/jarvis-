@echo off
title Friday - repair
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\repair.ps1"
echo.
pause
