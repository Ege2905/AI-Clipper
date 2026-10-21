@echo off
chcp 65001 >nul
title Clip Studio
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python fehlt. Bitte installieren und dabei "Add Python to PATH" anhaken:
  echo  https://www.python.org/downloads/
  start https://www.python.org/downloads/
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    echo  ffmpeg wird installiert ^(einmalig^)...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  )
)

where deno >nul 2>nul
if errorlevel 1 (
  if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe" (
    echo  deno wird installiert ^(einmalig, fuer YouTube-Downloads^)...
    winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
  )
)

if not exist venv (
  echo  Erster Start: richte Clip Studio ein ^(dauert ein paar Minuten^)...
  python -m venv venv
)
call venv\Scripts\activate.bat
python -m pip install -q --upgrade pip

:run
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo  Installation fehlgeschlagen - siehe Meldungen oben.
  pause
  exit /b 1
)
python app.py
if %errorlevel%==42 (
  echo.
  echo  Update installiert - Clip Studio startet neu...
  set CLIPSTUDIO_NO_BROWSER=1
  goto run
)
pause
