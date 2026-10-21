@echo off
chcp 65001 >nul
title Clip Studio - Autostart einrichten
cd /d "%~dp0"
echo.
echo  Clip Studio als Hintergrund-Dienst einrichten:
echo  - startet automatisch, wenn du dich bei Windows anmeldest
echo  - laeuft unsichtbar, kein schwarzes Fenster
echo  - installiert Updates von GitHub selbststaendig
echo.
curl -s -m 2 http://127.0.0.1:7860/api/ping >nul 2>nul
if not errorlevel 1 (
  echo  Clip Studio laeuft gerade schon. Bitte das andere schwarze Fenster schliessen
  echo  ^(oder stop_server.bat ausfuehren^) und dann hier eine Taste druecken.
  pause
)
call scripts\setup.bat
if errorlevel 1 ( pause & exit /b 1 )
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Clip Studio.lnk'); $s.TargetPath='wscript.exe'; $s.Arguments='\"%~dp0scripts\run_hidden.vbs\"'; $s.WorkingDirectory='%~dp0'; $s.Description='Clip Studio Server'; $s.Save()"
wscript.exe "%~dp0scripts\run_hidden.vbs"
echo  Warte auf den Server...
for /l %%i in (1,1,90) do (
  curl -s -m 2 http://127.0.0.1:7860/api/ping >nul 2>nul && goto ok
  timeout /t 2 /nobreak >nul
)
echo  Server antwortet noch nicht - schau in server.log nach.
pause
exit /b 1
:ok
echo.
echo  Fertig! Clip Studio laeuft jetzt im Hintergrund:  http://127.0.0.1:7860
echo  Lesezeichen anlegen. Stoppen: stop_server.bat - Autostart entfernen: autostart_entfernen.bat
start "" http://127.0.0.1:7860
pause
