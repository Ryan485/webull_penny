"""
Backtest runner CLI.

Examples:
    # Backtest the built-in viral watchlist over 6 months
    python backtest_runner.py --universe viral --start 2024-01-01 --end 2024-06-30

    # Backtest specific tickers
    python backtest_runner.py --tickers AMC GME BBBY CLOV --start 2023-06-01 --end 2023-12-31

    # Use your saved scan history as the universe
    python backtest_runner.py --universe history --start 2024-01-01 --end 2024-06-01

NOTE: yfinance only provides 1-minute data for the past 7 days.
      For longer backtests, the engine falls back to 5-minute candles.
"""
import argparse
import logging
import os
import sys
from datetime import datetime

import pytz

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from backtesting.engine import run_backtest
from data.scanner import VIRAL_WATCHLIST, load_historical_viral

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
ET = pytz.timezone("America/New_York")


def main():
    parser = argparse.ArgumentParser(description="CashCow Backtester")
    parser.add_argument(
        "--universe",
        choices=["viral", "history"],
        default=None,
        help="'viral' = built-in watchlist, 'history' = your saved Finviz scan history"
    )
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Custom list of tickers (overrides --universe)"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--capital", type=float, default=None,
        help="Starting capital (default from config)"
    )
    parser.add_argument(
        "--out", default="logs/backtest_results.csv",
        help="CSV output path (default: logs/backtest_results.csv)"
    )
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET)

    # Determine ticker universe
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    elif args.universe == "history":
        hist = load_historical_viral()
        tickers_set = set()
        for v in hist.values():
            tickers_set.update(v)
        tickers = sorted(tickers_set)
        print(f"Using {len(tickers)} tickers from saved scan history")
    else:
        tickers = VIRAL_WATCHLIST
        print(f"Using built-in viral watchlist ({len(tickers)} tickers)")

    if not tickers:
        print("No tickers found. Run the scanner first or provide --tickers.")
        sys.exit(1)

    print(f"\nBacktesting {len(tickers)} tickers: {args.start} → {args.end}")
    print(f"Tickers: {', '.join(tickers[:10])}{'...' if len(tickers) > 10 else ''}\n")

    import config
    capital = args.capital or config.ACCOUNT_SIZE
    result = run_backtest(tickers, start_dt, end_dt, initial_capital=capital)

    print(result.summary())

    # Strategy breakdown
    df = result.to_dataframe()
    if not df.empty:
        print("Strategy breakdown:")
        for strat, group in df.groupby("strategy"):
            wins = (group["pnl"] > 0).sum()
            total = len(group)
            total_pnl = group["pnl"].sum()
            print(f"  {strat:25s}  trades={total:3d}  wins={wins:3d}  "
                  f"win%={wins/total:.0%}  pnl=${total_pnl:+,.2f}")

        # Save CSV
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        df.to_csv(args.out, index=False)
        print(f"\nFull trade log saved to: {args.out}")


if __name__ == "__main__":
    main()
