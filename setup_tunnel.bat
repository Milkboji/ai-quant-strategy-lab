@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  AI Quant Lab - Cloudflare Tunnel setup
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_tunnel.ps1"
if errorlevel 1 ( echo [ERR] setup failed & pause & exit /b 1 )
echo [..] Restarting service with the tunnel ...
".venv\Scripts\python.exe" scripts\service.py stop >nul
wscript.exe "%~dp0tools\run_hidden.vbs" open
echo [OK] Done.
timeout /t 20
