@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" scripts\fetch_crypto_history.py > data\history_fetch.log 2>&1
