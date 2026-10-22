@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set FT_LEVERAGE=2
set FT_SHORT=0
echo [..] 转换本地行情...
".ftenv\Scripts\python.exe" prepare_data.py > backtest_result.txt 2>&1
echo [..] 回测中（约 1 分钟）...
".ftenv\Scripts\python.exe" ft_offline.py backtesting --config config_bt.json --userdir user_data --strategy TrendCoreFutures --timerange 20250523- --timeframe-detail 1h >> backtest_result.txt 2>&1
type backtest_result.txt
echo.
echo [OK] 结果已保存到 backtest_result.txt
pause
