@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  AI Quant Lab - install background autostart
echo ============================================
if not exist ".venv\Scripts\pythonw.exe" (
  echo [..] Python environment not found, running first-time setup via start.bat ...
  call start.bat
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_autostart.ps1"
if errorlevel 1 ( echo [ERR] install failed & pause & exit /b 1 )
echo [..] Starting service in background and opening the dashboard ...
wscript.exe "%~dp0tools\run_hidden.vbs" open
echo.
echo [OK] Done. From now on:
echo      - the system starts automatically after you log in to Windows
echo      - a background check every 5 minutes restarts it if it stopped
echo      - double-click the desktop icon "AI量化交易" to open it
echo      - stop.bat stops it and pauses the auto-restart until you open it again
echo.
timeout /t 15
