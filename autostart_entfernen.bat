@echo off
chcp 65001 >nul
cd /d "%~dp0"
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Clip Studio.lnk" >nul 2>nul
call stop_server.bat
echo  Autostart entfernt.
pause
