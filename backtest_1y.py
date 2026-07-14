"""
One-year backtest of the current live bot logic against historical viral penny stocks.

Phase 1 — build the universe:
  Pull ~1 year of split-adjusted daily bars (SIP feed) for all active US equities.
  For each trading day, select the penny-stock top gainers the live scanner
  would have flagged:  $0.50-$10, close-over-prev-close >= +20% (or intraday
  high >= +35%), volume >= 5M shares, dollar volume >= $3M.  Top 10 per day.
  Cached to logs/reports/viral_events_1y.csv (delete to rescan).

Phase 2 — replay:
  For each (date, ticker) event, fetch that day's 1-minute SIP bars and run
  the same simulate_day() used in backtest_viral.py (mirrors main.py exactly:
  10:00 gate, VWAP filter, strategy stop/target overrides, trailing stops,
  per-ticker caps, 15:30 close).
  Trades append to logs/reports/backtest_1y_trades.csv with per-day
  checkpointing (safe to interrupt and rerun).

Caveats:
  - Universe built from currently-active Alpaca assets → stocks delisted during
    the year are missing (mild survivorship bias, likely flatters results).
  - SIP minute bars (fuller candles than the IEX feed the live bot sees).
  - No slippage; fills at bar close / exact stop prices.

Usage: py -3.12 backtest_1y.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import csv
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import pytz

import config
from data.indicators import compute_all
from backtest_viral import simulate_day

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

EVENTS_CSV = "logs/reports/viral_events_1y.csv"
TRADES_CSV = "logs/reports/backtest_1y_trades.csv"
DONE_FILE = "logs/reports/backtest_1y_done.txt"

START = "2025-07-07"
END = "2026-07-02"

# Scanner-equivalent filters (SIP = real volume, unlike the live IEX proxy)
PRICE_MIN, PRICE_MAX = 0.5, 10.0
MIN_CHANGE_CLOSE = 0.20      # close vs prev close
MIN_CHANGE_HIGH = 0.35       # intraday high vs prev close (catches spike-and-fade)
MIN_SHARES = 5_000_000
MIN_DOLLAR_VOL = 3_000_000
TOP_N_PER_DAY = 10

TRADE_FIELDS = ["date", "ticker", "strategy", "entry_time", "exit_time",
                "entry", "exit", "shares", "pnl", "pnl_pct", "reason", "notes"]


# ── Alpaca REST helpers ───────────────────────────────────────────────────────

def _get(url: str, retries: int = 4):
    headers = {"APCA-API-KEY-ID": config.ALPACA_API_KEY,
               "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3)
    raise RuntimeError("unreachable")


def fetch_assets() -> list:
    data = _get("https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity")
    syms = [a["symbol"] for a in data
            if a.get("tradable") and a.get("exchange") in ("NASDAQ", "NYSE", "AMEX")
            and "." not in a["symbol"] and "/" not in a["symbol"]]
    return sorted(set(syms))


def fetch_daily_bars(symbols: list) -> dict:
    """Multi-symbol daily bars for the full range. Returns {sym: [bar,...]}."""
    out = defaultdict(list)
    base = ("https://data.alpaca.markets/v2/stocks/bars"
            f"?timeframe=1Day&start={START}&end={END}"
            "&feed=sip&adjustment=split&limit=10000")
    url = base + "&symbols=" + urllib.parse.quote(",".join(symbols))
    token = None
    while True:
        u = url + (f"&page_token={token}" if token else "")
        data = _get(u)
        for sym, bars in (data.get("bars") or {}).items():
            out[sym].extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
    return out


def fetch_minute_bars_day(symbols: list, date_str: str) -> dict:
    """1-min SIP bars 04:00-16:00 ET for one day. Returns {sym: DataFrame}."""
    day = ET.localize(datetime.strptime(date_str, "%Y-%m-%d"))
    start = urllib.parse.quote(day.replace(hour=4).isoformat())
    end = urllib.parse.quote(day.replace(hour=16).isoformat())
    base = ("https://data.alpaca.markets/v2/stocks/bars"
            f"?timeframe=1Min&start={start}&end={end}&feed=sip&limit=10000")
    url = base + "&symbols=" + urllib.parse.quote(",".join(symbols))
    raw = defaultdict(list)
    token = None
    while True:
        u = url + (f"&page_token={token}" if token else "")
        data = _get(u)
        for sym, bars in (data.get("bars") or {}).items():
            raw[sym].extend(bars)
        token = data.get("next_page_token")
        if not token:
            break
    out = {}
    for sym, bars in raw.items():
        if len(bars) < 60:
            continue
        df = pd.DataFrame(bars)
        df.index = pd.to_datetime(df["t"]).dt.tz_convert(ET)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                "c": "close", "v": "volume"})
        out[sym] = df[["open", "high", "low", "close", "volume"]].sort_index()
    return out


# ── Phase 1: build the historical viral universe ─────────────────────────────

def build_events() -> pd.DataFrame:
    if os.path.exists(EVENTS_CSV):
        ev = pd.read_csv(EVENTS_CSV, dtype={"date": str})
        print(f"[phase1] cached: {len(ev)} events from {EVENTS_CSV}")
        return ev

    print("[phase1] fetching asset list...")
    symbols = fetch_assets()
    print(f"[phase1] {len(symbols)} active symbols; fetching 1y daily bars in batches...")

    events = []
    BATCH = 200
    for bi in range(0, len(symbols), BATCH):
        batch = symbols[bi:bi + BATCH]
        try:
            daily = fetch_daily_bars(batch)
        except Exception as e:
            print(f"[phase1] batch {bi//BATCH} failed: {e}")
            continue
        for sym, bars in daily.items():
            for j in range(1, len(bars)):
                prev_c = bars[j - 1]["c"]
                b = bars[j]
                if prev_c <= 0:
                    continue
                chg_c = b["c"] / prev_c - 1
                chg_h = b["h"] / prev_c - 1
                if not (PRICE_MIN <= b["c"] <= PRICE_MAX):
                    continue
                if chg_c < MIN_CHANGE_CLOSE and chg_h < MIN_CHANGE_HIGH:
                    continue
                if b["v"] < MIN_SHARES or b["c"] * b["v"] < MIN_DOLLAR_VOL:
                    continue
                events.append({
                    "date": b["t"][:10], "ticker": sym,
                    "close": round(b["c"], 4),
                    "chg_close_pct": round(chg_c * 100, 1),
                    "chg_high_pct": round(chg_h * 100, 1),
                    "volume": int(b["v"]),
                })
        done = min(bi + BATCH, len(symbols))
        print(f"[phase1] {done}/{len(symbols)} symbols scanned, {len(events)} events so far")

    ev = pd.DataFrame(events)
    if ev.empty:
        raise RuntimeError("No events found — check filters/data access")
    # Top N per day by close change (what the scanner watchlist would hold)
    ev = (ev.sort_values(["date", "chg_close_pct"], ascending=[True, False])
            .groupby("date").head(TOP_N_PER_DAY).reset_index(drop=True))
    os.makedirs(os.path.dirname(EVENTS_CSV), exist_ok=True)
    ev.to_csv(EVENTS_CSV, index=False)
    print(f"[phase1] saved {len(ev)} events across {ev['date'].nunique()} days -> {EVENTS_CSV}")
    return ev


# ── Phase 2: replay ───────────────────────────────────────────────────────────

def load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE) as f:
        return set(line.strip() for line in f if line.strip())


def run_replay(ev: pd.DataFrame) -> None:
    done = load_done()
    by_day = {d: sorted(g["ticker"].tolist()) for d, g in ev.groupby("date")}
    days = [d for d in sorted(by_day) if d not in done]
    print(f"[phase2] {len(days)} days to simulate ({len(done)} already done)")

    write_header = not os.path.exists(TRADES_CSV)
    t0 = time.time()
    for k, date_str in enumerate(days):
        tickers = by_day[date_str]
        try:
            bars = fetch_minute_bars_day(tickers, date_str)
        except Exception as e:
            print(f"[phase2] {date_str}: bar fetch failed ({e}), skipping")
            continue
        day_trades = []
        for sym in tickers:
            df = bars.get(sym)
            if df is None:
                continue
            try:
                simulate_day(sym, date_str, compute_all(df), day_trades)
            except Exception as e:
                print(f"[phase2] {date_str} {sym}: sim error {e}")
        with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if write_header:
                w.writeheader()
                write_header = False
            w.writerows(day_trades)
        with open(DONE_FILE, "a") as f:
            f.write(date_str + "\n")
        elapsed = time.time() - t0
        rate = (k + 1) / elapsed * 60
        print(f"[phase2] {date_str}: {len(bars)}/{len(tickers)} tickers had data, "
              f"{len(day_trades)} trades  ({k+1}/{len(days)}, {rate:.1f} days/min)")


# ── Report ────────────────────────────────────────────────────────────────────

def report() -> None:
    tdf = pd.read_csv(TRADES_CSV)
    if tdf.empty:
        print("No trades.")
        return
    wins = tdf[tdf.pnl > 0]
    losses = tdf[tdf.pnl <= 0]
    gross_p, gross_l = wins.pnl.sum(), abs(losses.pnl.sum())
    pf = gross_p / gross_l if gross_l > 0 else float("inf")

    print(f"\n{'='*66}")
    print(f"  1-YEAR BACKTEST — live logic vs historical viral pennies")
    print(f"  {tdf.date.min()} → {tdf.date.max()}  ({tdf.date.nunique()} trading days)")
    print(f"{'='*66}")
    print(f"  Trades       : {len(tdf)}")
    print(f"  Win rate     : {len(wins)/len(tdf):.0%}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total PnL    : ${tdf.pnl.sum():+,.2f}  (account ${config.ACCOUNT_SIZE:,.0f}, no compounding)")
    print(f"  Avg win      : ${wins.pnl.mean():+.2f}")
    print(f"  Avg loss     : ${losses.pnl.mean():+.2f}")
    print(f"  Expectancy   : ${tdf.pnl.mean():+.2f}/trade")
    print(f"  Profit factor: {pf:.2f}")

    print(f"\n  By strategy:")
    for strat, g in tdf.groupby("strategy"):
        w = (g.pnl > 0).sum()
        print(f"    {strat:22s} {len(g):4d} trades  {w:4d} wins ({w/len(g):.0%})  ${g.pnl.sum():+12.2f}")

    print(f"\n  By exit reason:")
    for reason, g in tdf.groupby("reason"):
        print(f"    {reason:22s} {len(g):4d} trades  ${g.pnl.sum():+12.2f}")

    tdf["month"] = tdf.date.str[:7]
    print(f"\n  By month:")
    for m, g in tdf.groupby("month"):
        w = (g.pnl > 0).sum()
        print(f"    {m}   {len(g):4d} trades  {w/len(g):.0%} wins  ${g.pnl.sum():+12.2f}")

    print(f"\n  Trade detail: {TRADES_CSV}")


if __name__ == "__main__":
    ev = build_events()
    run_replay(ev)
    report()
