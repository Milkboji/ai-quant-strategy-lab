@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".ftenv\Scripts\python.exe" py -3 -m venv .ftenv
".ftenv\Scripts\python.exe" -m pip install -U pip freqtrade
echo [OK] Freqtrade 安装完成
pause
