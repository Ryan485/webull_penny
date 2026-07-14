@echo off
echo CashCow Backtester
echo Usage examples:
echo   py -3.12 backtest_runner.py --universe viral --start 2024-01-01 --end 2024-06-30
echo   py -3.12 backtest_runner.py --tickers AMC GME BBBY --start 2023-06-01 --end 2023-12-31
echo.
py -3.12 backtest_runner.py %*
pause
