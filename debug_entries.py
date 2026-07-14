"""Quick debug: test scanner and bar fetching."""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.basicConfig(level=logging.WARNING)

from data.scanner import scan_movers as scan_webull
from data.market_data import get_live_bars, is_market_open
from data.indicators import compute_all
from strategies.double_bottom import DoubleBottom
from strategies.trend_reversal import TrendReversal
from strategies.resistance_breakout import ResistanceBreakout

STRATEGIES = [DoubleBottom(), TrendReversal(), ResistanceBreakout()]

print(f"Market open: {is_market_open()}")
print("Getting watchlist...")
tickers = scan_webull()
print(f"Got {len(tickers)} tickers: {tickers[:10]}")
print()

hits = 0
no_data = 0
for t in tickers[:20]:
    df = get_live_bars(t)
    if df is None or len(df) < 50:
        no_data += 1
        print(f"  {t}: NO DATA (df={df is not None and len(df) or 'None'})")
        continue

    df2 = compute_all(df)
    atr = df2["atr"].iloc[-1] if "atr" in df2.columns else None
    rsi = df2["rsi"].iloc[-1] if "rsi" in df2.columns else None
    close = df2["close"].iloc[-1]
    atr_s = f"{atr:.4f}" if atr else "N/A"
    rsi_s = f"{rsi:.1f}" if rsi else "N/A"
    print(f"  {t}: {len(df2)} bars, close={close:.4f}, ATR={atr_s}, RSI={rsi_s}")

    for strat in STRATEGIES:
        sig = strat.evaluate(t, df2, prior_close=None)
        if sig.triggered:
            print(f"    SIGNAL! {strat.name} score={sig.score} entry={sig.entry_price}")
            hits += 1
        else:
            print(f"    {strat.name}: score={sig.score} notes={sig.notes[:60] if sig.notes else ''}")

print(f"\nSummary: {hits} signals from {len(tickers[:20]) - no_data} tickers with data ({no_data} no data)")
