@echo off
:: ZeroBot Pro — Backtest Runner (Windows)
:: ══════════════════════════════════════════
:: HOW TO USE:
::   Double-click = test all default symbols
::
::   Or from terminal (for specific symbols/strategies):
::   run_backtest.bat RELIANCE TCS INFY
::   run_backtest.bat RELIANCE --strategy supertrend
::   run_backtest.bat --strategy momentum --windows 12

title ZeroBot Backtester

echo.
echo  ════════════════════════════════════════
echo    ZeroBot Walk-Forward Backtester
echo  ════════════════════════════════════════
echo.
echo  Tip: Pass symbols as arguments, e.g.:
echo    run_backtest.bat RELIANCE TCS INFY
echo.

python -X utf8 run_backtest.py %*

echo.
echo Done. Press any key to close.
pause
