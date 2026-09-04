@echo off
title Surau Setia Eco Glades Payment Server
cd /d "%~dp0"
powershell.exe -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://localhost:3003/api/health' -TimeoutSec 2; if ($r.service -eq 'surau-payment') { exit 0 } } catch {}; exit 1"
if %errorlevel% equ 0 (
  echo The payment server is already running. Opening the website...
  start "" "http://localhost:3003"
  timeout /t 2 /nobreak >nul
  exit /b 0
)
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Milliseconds 900; Start-Process 'http://localhost:3003'"
"C:\Program Files\nodejs\node.exe" server.mjs
echo.
echo The payment server has stopped. Keep this window open while the site is in use.
pause
