@echo off
rem Power / Windows Update settings. Double-click: show current settings only. "power_setup.bat apply": change them (asks for admin).
if /I "%1"=="apply" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0tools\power_setup.ps1\" -Apply'"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\power_setup.ps1"
  pause
)
