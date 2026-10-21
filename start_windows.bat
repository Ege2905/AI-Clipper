@echo off
chcp 65001 >nul
title Clip Studio
cd /d "%~dp0"
rem Already running (e.g. in the background)? Then just open it in the browser.
curl -s -m 2 http://127.0.0.1:7860/api/ping >nul 2>nul
if not errorlevel 1 (
  start "" http://127.0.0.1:7860
  exit /b 0
)
echo.
echo  Clip Studio wird vorbereitet...
call scripts\setup.bat
if errorlevel 1 (
  echo  Installation fehlgeschlagen - siehe Meldungen oben.
  pause
  exit /b 1
)
venv\Scripts\python launcher.py
pause
