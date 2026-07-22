"""
Backtest the CURRENT live bot logic against past viral stocks.

Universe: every (date, ticker) recorded in logs/viral_research_summary.csv
Data:     Alpaca historical 1-minute IEX bars (same feed the live bot uses)

Mirrors main.py exactly:
  - Strategies: DoubleBottom, TrendReversal, ResistanceBreakout
  - Entry gates: after 10:00 ET, before 15:30 ET, close >= VWAP
  - Stop/target from strategy overrides, else calc_stop_and_target
  - Trailing stops: trail 0.75R below high-water once +1R is reached
  - Per-ticker caps: max 3 trades/day, max 2 stops/day, 30-min cooldown after exit
  - All positions closed at 15:30 ET

Known simplifications (all conservative or neutral):
  - Each ticker-day simulated independently (no cross-ticker MAX_POSITIONS cap,
    so trade COUNT may run higher than live; per-trade results unaffected)
  - Fills at bar close (entry) / stop price (stops); no slippage modeled
  - Trailing stop updates once per 1-min bar vs every 10s live

Usage: py -3.12 backtest_viral.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import csv
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import pytz

import config
from data.indicators import compute_all
from data.market_data import get_bars_alpaca
from strategies.double_bottom import DoubleBottom
from strategies.trend_reversal import TrendReversal
from strategies.resistance_breakout import ResistanceBreakout
from trading.risk_manager import calc_stop_and_target, calc_position_size

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

STRATEGIES = [DoubleBottom(), TrendReversal(), ResistanceBreakout()]

SUMMARY_CSV = "logs/viral_research_summary.csv"
OUT_CSV = "logs/reports/backtest_viral_trades.csv"

ENTRY_START = (10, 0)     # no entries before 10:00 ET
ENTRY_CUTOFF = (15, 30)   # no NEW entries at/after 15:30 ET
_eod = os.environ.get("BT_EOD_HHMM", "1530")
EOD_CUTOFF = (int(_eod[:2]), int(_eod[2:]))
                    # force-flat time. Default 15:30 (live behavior). Sweep
                    # 2026-07-17 (owner: don't dump a working trade at 15:30,
                    # keep managing it) — try 1545 / 1555.
COOLDOWN_MINS = int(os.environ.get("BT_COOLDOWN_MINS", "10"))
                    # 30 -> 10 with portfolio.py (2026-07-16, owner directive
                    # "remove the cooldown"): 30min PF 2.81 / 0min PF 2.78 /
                    # 10min PF 3.32 (+$34.9K). The 30-min lock cost VMAR's
                    # 10:25 recovery W (07-14) and TGHL's 12:33 window (07-16);
                    # zero cooldown re-buys still-falling names.
MAX_TRADES_PER_DAY = 3
MAX_STOPS_PER_DAY = int(os.environ.get("BT_MAX_STOPS_PER_DAY", "3"))
                    # raised 2 -> 3 with portfolio.py (2026-07-16, TGHL): the
                    # 2-stop blacklist blocked the 14:28 ET rb breakout (7x
                    # vol) that ran 1.46 -> 1.576. Sweep: cap 2 PF 2.62 /
                    # +$25.1K vs cap 3 PF 2.81 / +$28.5K (uncapped identical —
                    # the 3-trades/day cap binds first).


def load_universe() -> dict:
    """Return {date_str: set(tickers)} from the viral research summary."""
    universe = defaultdict(set)
    with open(SUMMARY_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue              # corrupted row (live bot appends mid-write)
            if d.weekday() >= 5:      # weekend records (pre-market scans) — skip
                continue
            universe[row["date"]].add(row["ticker"])
    return dict(sorted(universe.items()))


def fetch_day_bars(ticker: str, date_str: str):
    day = ET.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    start = day.replace(hour=4)
    end = day.replace(hour=16)
    df = get_bars_alpaca(ticker, start, end, timeframe="1Min")
    if df is None or len(df) < 60:
        return None
    return compute_all(df)


def simulate_day(ticker: str, date_str: str, df: pd.DataFrame, trades: list) -> None:
    position = None          # dict with entry state
    exits_today = 0
    stops_today = 0
    cooldown_until = None

    for i in range(50, len(df)):
        ts = df.index[i]
        row = df.iloc[i]
        hhmm = (ts.hour, ts.minute)
        eod = hhmm >= EOD_CUTOFF

        # ── Exit handling ────────────────────────────────────────────────
        if position:
            p = position
            exit_price = exit_reason = None

            # Check stop against this bar's LOW using the stop as of the
            # previous bar (trail updates after the check — conservative).
            if row["low"] <= p["stop"]:
                exit_price, exit_reason = p["stop"], (
                    "trailing_stop" if p["stop"] >= p["entry"] else "stop_loss")
            elif p.get("tp") and row["high"] >= p["target"]:
                exit_price, exit_reason = p["target"], "take_profit"
            elif eod:
                exit_price, exit_reason = row["close"], "eod_close"

            if exit_reason:
                comm = round(config.ibkr_commission(p["shares"], p["entry"])
                             + config.ibkr_commission(p["shares"], exit_price), 2)
                pnl = (exit_price - p["entry"]) * p["shares"] - comm
                trades.append({
                    "date": date_str, "ticker": ticker,
                    "strategy": p["strategy"],
                    "entry_time": str(p["entry_ts"])[11:16],
                    "exit_time": str(ts)[11:16],
                    "entry": round(p["entry"], 4),
                    "exit": round(exit_price, 4),
                    "shares": p["shares"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (p["entry"] * p["shares"]) * 100, 2),
                    "reason": exit_reason,
                    "notes": p["notes"][:60],
                    "commission": comm,
                })
                exits_today += 1
                if exit_reason == "stop_loss":
                    stops_today += 1
                cooldown_until = ts + timedelta(minutes=COOLDOWN_MINS)
                position = None
            else:
                # Update trailing state from this bar's high
                p["hw"] = max(p["hw"], row["high"])
                r = p["risk"]
                if r > 0 and p["hw"] >= p["entry"] + r:
                    trail = round(p["hw"] - 0.75 * r, 4)
                    if trail > p["stop"]:
                        p["stop"] = trail
            continue

        # ── Entry gates (mirror check_entries) ───────────────────────────
        if hhmm >= ENTRY_CUTOFF or hhmm < ENTRY_START:
            continue
        if exits_today >= MAX_TRADES_PER_DAY or stops_today >= MAX_STOPS_PER_DAY:
            continue
        if cooldown_until is not None and ts < cooldown_until:
            continue

        vwap = row.get("vwap")
        below_vwap = bool(vwap == vwap and vwap and row["close"] < vwap)

        window = df.iloc[: i + 1]
        best = None
        for strat in STRATEGIES:
            sig = strat.evaluate(ticker, window, prior_close=None)
            if not sig.triggered:
                continue
            if below_vwap and not sig.ignore_vwap:
                continue
            if best is None or sig.score > best.score:
                best = sig

        if best is None:
            continue
        atr = row.get("atr")
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row["close"]
        if best.stop_price is not None and best.target_price is not None:
            stop, target = best.stop_price, best.target_price
        else:
            stop, target = calc_stop_and_target(entry, atr)
        if stop >= entry:
            continue
        shares = calc_position_size(config.ACCOUNT_SIZE, entry, stop)
        if shares < 1:
            continue

        position = {
            "entry": entry, "stop": stop, "target": target,
            "shares": shares, "strategy": best.strategy,
            "notes": best.notes or "", "entry_ts": ts,
            "risk": max(entry - stop, 0.0), "hw": entry,
            "tp": best.take_profit,
        }

    # Force close at end of data
    if position:
        p = position
        last = df["close"].iloc[-1]
        comm = round(config.ibkr_commission(p["shares"], p["entry"])
                     + config.ibkr_commission(p["shares"], last), 2)
        pnl = (last - p["entry"]) * p["shares"] - comm
        trades.append({
            "date": date_str, "ticker": ticker, "strategy": p["strategy"],
            "entry_time": str(p["entry_ts"])[11:16],
            "exit_time": str(df.index[-1])[11:16],
            "entry": round(p["entry"], 4), "exit": round(last, 4),
            "shares": p["shares"], "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (p["entry"] * p["shares"]) * 100, 2),
            "reason": "eod_close", "notes": p["notes"][:60],
            "commission": comm,
        })


def main():
    universe = load_universe()
    n_days = len(universe)
    n_pairs = sum(len(t) for t in universe.values())
    print(f"Universe: {n_pairs} ticker-days across {n_days} dates\n")

    trades = []
    no_data = []
    for date_str, tickers in universe.items():
        for ticker in sorted(tickers):
            df = fetch_day_bars(ticker, date_str)
            if df is None:
                no_data.append(f"{date_str} {ticker}")
                continue
            n_before = len(trades)
            simulate_day(ticker, date_str, df, trades)
            n_new = len(trades) - n_before
            status = f"{n_new} trade(s)" if n_new else "-"
            print(f"  {date_str} {ticker:6s} {len(df):4d} bars  {status}")

    if no_data:
        print(f"\nNo data for {len(no_data)} ticker-days: {', '.join(no_data)}")

    if not trades:
        print("\nNo trades generated.")
        return

    tdf = pd.DataFrame(trades)
    import os
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    tdf.to_csv(OUT_CSV, index=False)

    wins = tdf[tdf.pnl > 0]
    losses = tdf[tdf.pnl <= 0]
    gross_p = wins.pnl.sum()
    gross_l = abs(losses.pnl.sum())
    pf = gross_p / gross_l if gross_l > 0 else float("inf")

    print(f"\n{'='*64}")
    print(f"  BACKTEST — current live logic vs past viral stocks")
    print(f"{'='*64}")
    print(f"  Trades       : {len(tdf)}")
    print(f"  Win rate     : {len(wins)/len(tdf):.0%}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total PnL    : ${tdf.pnl.sum():+,.2f}  (on ${config.ACCOUNT_SIZE:,.0f} account)")
    print(f"  Avg win      : ${wins.pnl.mean():+.2f}" if len(wins) else "  Avg win      : n/a")
    print(f"  Avg loss     : ${losses.pnl.mean():+.2f}" if len(losses) else "  Avg loss     : n/a")
    print(f"  Expectancy   : ${tdf.pnl.mean():+.2f}/trade")
    print(f"  Profit factor: {pf:.2f}")

    print(f"\n  By strategy:")
    for strat, g in tdf.groupby("strategy"):
        w = (g.pnl > 0).sum()
        print(f"    {strat:22s} {len(g):3d} trades  {w}/{len(g)} wins  ${g.pnl.sum():+9.2f}")

    print(f"\n  By exit reason:")
    for reason, g in tdf.groupby("reason"):
        print(f"    {reason:22s} {len(g):3d} trades  ${g.pnl.sum():+9.2f}")

    print(f"\n  Trade detail saved to {OUT_CSV}")


if __name__ == "__main__":
    main()
