@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
".venv\Scripts\python.exe" -m pytest -q tests
pause
