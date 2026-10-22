@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo [..] First run: creating Python virtual environment .venv
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [ERR] Python 3.10+ not found. Install from https://www.python.org/downloads/ and tick "Add to PATH".
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 ( echo [ERR] pip install failed & pause & exit /b 1 )
)
".venv\Scripts\python.exe" scripts\service.py start
if errorlevel 1 pause
