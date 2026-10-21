@echo off
rem Installs everything Clip Studio needs (Python packages, ffmpeg, deno). Safe to run repeatedly.
cd /d "%~dp0.."
where python >nul 2>nul
if errorlevel 1 (
  echo  Python fehlt. Bitte installieren und "Add Python to PATH" anhaken: https://www.python.org/downloads/
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
  echo  ffmpeg wird installiert ^(einmalig^)...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
where deno >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe" (
  echo  deno wird installiert ^(einmalig, fuer YouTube-Downloads^)...
  winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
)
if not exist venv (
  echo  Richte Python-Umgebung ein ^(einmalig, ein paar Minuten^)...
  python -m venv venv
  venv\Scripts\python -m pip install -q --upgrade pip
)
venv\Scripts\python -m pip install -q -r requirements.txt
exit /b %errorlevel%
