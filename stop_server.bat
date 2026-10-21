@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo stop> stop.flag
if exist launcher.pid for /f %%p in (launcher.pid) do taskkill /PID %%p /T /F >nul 2>nul
if exist app.pid for /f %%p in (app.pid) do taskkill /PID %%p /T /F >nul 2>nul
del launcher.pid app.pid >nul 2>nul
echo  Clip Studio wurde gestoppt.
timeout /t 3 >nul
