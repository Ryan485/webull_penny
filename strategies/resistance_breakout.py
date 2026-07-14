"""
Strategy 4: Resistance Breakout (all day)

Rules (1-minute candles, rolling 120-bar window):
  - Resistance = a price level formed by 2+ swing highs (5-bar pivots)
    clustered within 0.5%, with touches at least 10 bars apart
  - Price must have respected the level: closes stayed below it, except a
    FRESH break (first close above within the last 3 bars) is still tradeable
  - Entry when the current bar CLOSES above resistance * 1.002
    but no more than 2% above it (don't chase extended breakouts)
    (widened 2026-07-06: the old 1.005-1.01 band was one 1m-candle wide and
    viral pennies gapped straight over it - zero fills in backtests)
  - Breakout candle volume must be at least 1.5x the 20-bar average
  - Stop: resistance - 0.5 ATR (old resistance becomes support)
  - Target: entry + 2 * (entry - stop), i.e. 2R
  - RETEST mode (added 2026-07-07): a level broken within the last 30 bars
    is not spent — if price pulled back to within 2 ATR of it, buy a green
    bar bouncing 0.5 ATR off the pullback low (no volume gate; pullbacks
    are quiet by nature). Stop: 0.5 ATR below the pullback low.
"""
import os
from typing import List, Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal


WINDOW = 120
MIN_BARS = 45                    # 2 touches >=10 bars apart fit in far less than
                                 # 120 bars; tickers with no premarket data would
                                 # otherwise be invisible before 11:30 ET
PIVOT = 5
CLUSTER_PCT = 0.005              # swing highs within 0.5% form one level
MIN_TOUCH_GAP = 10               # touches must be ≥10 bars apart
MIN_TOUCHES = int(os.environ.get("RB_MIN_TOUCHES", "2"))  # owner asked (2026-07-14,
                                 # LHAI) whether 2 tests of a level is enough —
                                 # sweepable so the answer comes from a backtest
BREAKOUT_BUFFER = 1.002          # close must clear resistance by 0.2%
MAX_ENTRY_EXTENSION = 1.02       # …but not more than 2% above (don't chase)
FRESH_BREAK_BARS = 3             # may enter up to 3 bars after the first break
MIN_VOL_MULT = 1.5               # breakout volume ≥ 1.5x average
STOP_ATR_MULT = 0.5
TARGET_R = 2.0

# Pullback-retest entry (added 2026-07-07): a level whose break has gone
# stale is not spent if price pulled back to it — buy the bounce off the
# pullback low while the break is still recent.
RETEST_MAX_BARS = 30             # break must be within the last 30 bars
RETEST_MAX_DEPTH_ATR = 2.0       # pullback may dip at most 2 ATR below the level
RETEST_CONFIRM_ATR = 0.5         # close must be >= pullback low + 0.5 ATR

# Experiment flag (2026-07-14, LHAI): a box-top retest below session VWAP
# (level 1.252 vs VWAP 1.311 dragged up by the 9:30 spike) is the entry the
# owner wants, but the per-signal VWAP gate blocks it. Same argument that
# won for early W entries (below-VWAP structure entry with a tight stop).
# Default 0 = frozen behavior; backtest before ever enabling live.
RETEST_IGNORE_VWAP = os.environ.get("RB_RETEST_IGNORE_VWAP", "0") == "1"


PIVOT_TIE_PCT = 0.001            # equal highs ARE the level — ties don't disqualify


def _swing_highs(highs) -> List[int]:
    out = []
    for i in range(PIVOT, len(highs) - PIVOT):
        pivot = highs[i]
        cap = pivot * (1 + PIVOT_TIE_PCT)
        if all(highs[i - k] <= cap and highs[i + k] <= cap
               for k in range(1, PIVOT + 1)):
            out.append(i)
    return out


def _find_resistance_levels(df: pd.DataFrame) -> List[tuple]:
    """
    Return all valid resistance levels in the window as (level, first_break)
    tuples, where first_break is the window index of the first close above
    the level (None if it has never broken).
    A level needs 2+ swing-high touches within CLUSTER_PCT,
    spaced ≥ MIN_TOUCH_GAP bars, and closes must have stayed below it
    between the first touch and the break (excluding the current bar).
    """
    window = df.iloc[-WINDOW:].reset_index(drop=True)
    highs = window["high"].values
    closes = window["close"].values

    pivots = _swing_highs(highs)
    if len(pivots) < 2:
        return []

    levels = []
    for a_idx, a in enumerate(pivots):
        touches = [a]
        level = highs[a]
        for b in pivots[a_idx + 1:]:
            if abs(highs[b] - level) / level <= CLUSTER_PCT \
                    and b - touches[-1] >= MIN_TOUCH_GAP:
                touches.append(b)
        if len(touches) < MIN_TOUCHES:
            continue
        level = float(max(highs[t] for t in touches))
        # first close above the level AFTER the level fully formed (after
        # the last confirming touch). Searching from touches[0] was a
        # real-time logic bug (review 2026-07-14): a close between touch 1
        # and touch 2 counted as "breaking" a level that did not yet exist.
        # Fresh break = breakout entry; stale break = retest candidate;
        # very old break = level is spent.
        n = len(closes)
        first_break = next(
            (j for j in range(touches[-1] + 1, n - 1)
             if closes[j] > level * BREAKOUT_BUFFER),
            None,
        )
        if first_break is not None and (n - 1) - first_break > RETEST_MAX_BARS:
            continue
        if not any(abs(level - x) / x <= CLUSTER_PCT for x, _ in levels):
            levels.append((level, first_break))

    return levels


# Overhead-zone tolerance for target capping: penny wicks print sloppy
# (VTAK: 1.49 and 1.46 IEX wicks were one Webull wall at ~1.47, 2.01%
# apart). max(2%, 2 ATR) — same convention as the W Low2 tolerance.
RES_ZONE_PCT = 0.02
RES_ZONE_ATR = 2.0


def find_overhead_resistance(df: pd.DataFrame, close: float, atr: float = 0.0):
    """
    Nearest 2+ touch swing-high ZONE above close, ignoring break history.
    Returns the conservative BOTTOM of the zone (min touch high) — for a
    sell target you want the price the market actually paid twice, not the
    highest wick. Used by double_bottom to cap its target at known
    resistance (VTAK 2026-07-08: measured-move target 1.65 sat above a
    ~1.47 wall tagged twice; owner wants the sell placed at that wall).
    """
    window = df.iloc[-WINDOW:].reset_index(drop=True)
    highs = window["high"].values
    pivots = _swing_highs(highs)
    levels = []
    for a_idx, a in enumerate(pivots):
        tol = max(RES_ZONE_PCT * highs[a], RES_ZONE_ATR * atr)
        touches = [a]
        for b in pivots[a_idx + 1:]:
            if abs(highs[b] - highs[a]) <= tol \
                    and b - touches[-1] >= MIN_TOUCH_GAP:
                touches.append(b)
        if len(touches) < 2:
            continue
        lv = float(min(highs[t] for t in touches))
        if lv > close and not any(abs(lv - x) <= tol for x in levels):
            levels.append(lv)
    return min(levels) if levels else None


class ResistanceBreakout(BaseStrategy):
    name = "resistance_breakout"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        if len(df) < MIN_BARS:
            return Signal(ticker, self.name, 0, notes="insufficient_bars")

        row = df.iloc[-1]
        close = float(row["close"])
        atr = row.get("atr")
        if not atr or atr <= 0:
            return Signal(ticker, self.name, 0, entry_price=close, notes="no_atr")

        levels = _find_resistance_levels(df)
        if not levels:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="no_resistance")

        n_win = min(len(df), WINDOW)

        # Entry gate 1: close must sit in the breakout band of some level
        # with a FRESH break (or no break yet — this bar may be it) —
        # above it by ≥0.2% (confirmed break) but ≤2% (not extended/chasing)
        resistance = None
        for lv, brk in sorted(levels, reverse=True):
            if brk is not None and brk < n_win - 1 - FRESH_BREAK_BARS:
                continue  # stale break — retest candidate below
            if lv * BREAKOUT_BUFFER <= close <= lv * MAX_ENTRY_EXTENSION:
                resistance = lv
                break

        if resistance is None:
            # ── Retest mode: level broke recently, price pulled back to it.
            # Buy a green bar bouncing off the pullback low while the dip
            # stayed within RETEST_MAX_DEPTH_ATR of the level.
            lows = df.iloc[-WINDOW:]["low"].values
            is_green = close > float(row["open"])
            for lv, brk in sorted(levels, reverse=True):
                if brk is None or brk >= n_win - 1 - FRESH_BREAK_BARS:
                    continue  # never broke, or fresh break (handled above)
                if brk + 1 >= n_win:
                    continue
                pb_low = float(lows[brk + 1:].min())
                dipped = pb_low < lv
                shallow = pb_low >= lv - RETEST_MAX_DEPTH_ATR * float(atr)
                bounced = close >= pb_low + RETEST_CONFIRM_ATR * float(atr)
                not_extended = close <= lv * MAX_ENTRY_EXTENSION
                if is_green and dipped and shallow and bounced and not_extended:
                    stop = pb_low - STOP_ATR_MULT * float(atr)
                    return Signal(
                        ticker=ticker,
                        strategy=self.name,
                        score=5,
                        entry_price=close,
                        notes=(
                            f"rb_retest(res={lv:.3f},pb_low={pb_low:.3f},"
                            f"bars={(n_win - 1) - brk})"
                        ),
                        stop_price=stop,
                        target_price=close + TARGET_R * (close - stop),
                        ignore_vwap=RETEST_IGNORE_VWAP,
                    )
            nearest = min((lv for lv, _ in levels), key=lambda lv: abs(lv - close))
            state = "rb_extended" if close > nearest * MAX_ENTRY_EXTENSION else "rb_pending"
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"{state}(res={nearest:.3f},close={close:.3f})",
            )

        # Entry gate 2: volume confirmation
        avg_vol = row.get("avg_vol")
        vol = float(row.get("volume", 0) or 0)
        if not pd.notna(avg_vol) or avg_vol <= 0 or vol < MIN_VOL_MULT * avg_vol:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"rb_no_vol_confirm(res={resistance:.3f})",
            )

        stop = resistance - STOP_ATR_MULT * float(atr)
        target = close + TARGET_R * (close - stop)

        return Signal(
            ticker=ticker,
            strategy=self.name,
            score=5,
            entry_price=close,
            notes=(
                f"rb_breakout(res={resistance:.3f},vol={vol/avg_vol:.1f}x)"
            ),
            stop_price=stop,
            target_price=target,
        )
