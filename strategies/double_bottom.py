"""
Strategy 2: Double Bottom / W-Pattern Breakout (all day)

Rules (1-minute candles, rolling 60-bar window):
  - Swing low = pivot where lows on each side (3 bars) are strictly higher
  - Low1 must follow at least 5 candles of downward price movement
  - Bounce from Low1 must be at least max(0.3%, 1 ATR); bounce high = neckline
  - Low2 must form 10–50 candles after Low1
  - Low2 must be within max(0.2%, 0.75 ATR) of Low1
  - Low2 must not close more than 0.2% below Low1

Three entry modes (early added 2026-07-06, retest added 2026-07-07):
  1. EARLY (preferred): right after Low2's pivot confirms, enter on a green bar
     that closes >= 0.5 ATR above Low2 while still below the neckline.
     Buys near the second bottom instead of 8-10% higher at the neckline.
     Sets ignore_vwap=True: a W bottom is below session VWAP by nature, and
     the backtest showed below-VWAP early entries carry the edge (owner
     approved waiving the gate 2026-07-06 after seeing PF 1.37 vs 1.08).
     Stop: 0.5 ATR below Low2.
  1b. PULLBACK RETEST: the neckline broke within the last 30 bars, price
     dipped back below it holding the upper half of the W, and this bar
     bounces >= 0.5 ATR off the pullback low. Stop: 0.5 ATR below the
     pullback low (NOT below Low2 — the dip low is the new risk anchor).
  2. NECKLINE BREAK (fallback, the original): current bar closes above
     neckline * 1.0005 with above-average volume, not >1% extended.
     Stop: 0.5 ATR below Low2.
  - Target: measured move (neckline + (neckline - Low1))
"""
import os
from typing import Optional, Tuple

import pandas as pd

from strategies.base import BaseStrategy, Signal
from strategies.resistance_breakout import find_overhead_resistance


WINDOW = 120                     # 2-hour lookback so structural Ws are visible
MIN_BARS = 45                    # enough for MIN_GAP + pivots; tickers with no
                                 # premarket bars only have ~80 bars by 11:00 ET,
                                 # so demanding a full WINDOW blocked all
                                 # morning entries (found 2026-07-06, LUCY)
PIVOT = 5                        # bars that must be higher LEFT of a swing low
PIVOT_RIGHT = 3                  # confirmation bars RIGHT of the low — fewer, so
                                 # the early entry fires while price is still low
MIN_GAP = 20                     # Low1 → Low2 must be at least 20 min apart
MAX_GAP = 90                     # …and at most 90 min (still same session)
LOW_TOLERANCE_PCT = 0.005        # 0.5% — Low2 can be within 0.5% of Low1
LOW_ATR_TOLERANCE = 1.0          # …or within 1 ATR
LOW2_UNDERSHOOT_PCT = 0.003      # Low2 cannot close >0.3% below Low1
MIN_BOUNCE_PCT = 0.04            # 4% — pattern must have real depth
MIN_BOUNCE_ATR = 3.0             # …or ≥3 ATR
# MAX depth cap (added 2026-07-13, SOBR/JZXN): with no ceiling, a parabolic
# blow-off top gets read as the "neckline" and the entire spike-and-collapse
# as one giant W (SOBR: 24% deep, neck=0.838 was the 10:20 peak, -5.4%;
# JZXN 07-10: 52% deep, neck=3.22 was the spike top, -10.2%). A real W base
# is a few percent deep; past the cap it's a falling knife, not a bottom.
# Pct-only, no ATR escape: parabolic names have monster ATRs, so an
# "or N ATR" clause would exempt exactly the spikes the cap targets.
# 0.20 won the 2026-07-13 backtest sweep (PF 2.18 vs 1.94 uncapped, blocks
# both motivating losers); 0.10 is too tight and starts dropping winners.
MAX_BOUNCE_PCT = float(os.environ.get("DB_MAX_DEPTH_PCT", "0.20"))
DOWNTREND_LOOKBACK = 5
BREAKOUT_BUFFER = 1.0005         # close must clear neckline by 5 bps
MAX_ENTRY_EXTENSION = 1.01       # …but not more than 1% above neckline (don't chase)
STOP_ATR_MULT = 0.5

# Early entry (second-bottom confirmation)
EARLY_MAX_BARS_AFTER_LOW2 = 20   # bounce must confirm within 20 bars of Low2
EARLY_CONFIRM_ATR = 0.5          # close must be >= Low2 + 0.5 ATR (bounce underway)
EARLY_MAX_RETRACE = 0.5          # ...but below halfway to the neckline (stay "early")

# Pullback-retest entry (added 2026-07-07, SKIN): after a confirmed close
# above the neckline, viral pennies often dip back below it (onto the rising
# MAs) before the real move. Buy the bounce off that dip with a stop under
# the pullback low, instead of chasing the re-break at the neckline.
RETEST_MAX_BARS = 30             # neckline break must be within the last 30 bars
RETEST_MIN_HOLD = 0.5            # pullback low must hold above halfway between
                                 # Low2 and the neckline, else the W is failing

# Stochastic second-bottom gate (added 2026-07-07, BJDX): owner wants W
# entries taken only while Stoch(5,3,3) %K is turning UP off a fresh trough
# (the stoch "second bottom"), not after the oscillator has already sprung
# back to mid-range. BJDX 14:59 entered at K=57 off a 12.5 trough and lost
# -4.6%; every trigger bar that day fails this gate.
#   off  - no gate
#   turn - %K rising, trough <= STOCH_TROUGH_MAX within last 3 bars,
#          %K still <= STOCH_ENTRY_MAX
#   w    - "turn" plus an earlier trough <= STOCH_TROUGH_MAX within
#          STOCH_W_LOOKBACK bars with a peak >= STOCH_W_PEAK_MIN between
#          (a full stochastic W)
#   deeper - "turn" plus the fresh trough must be at least as deep as the
#          lowest %K of the prior STOCH_W_LOOKBACK bars (+ tolerance).
#          Owner rule from AGEN 2026-07-13 11:41 (-2.8%): "the middle stoch
#          is higher than the previous low — this cannot be W". A shallower
#          dip than the last one is a fake second bottom.
# Default "deeper" tol=5 chosen 2026-07-14 (owner rule, AGEN): sweep vs
# "turn" baseline PF 2.28/$231 -> deeper5 PF 2.75/$261 on 15 fewer trades;
# tol=0 over-tightens (PF 2.46, drops winners). Override with DB_STOCH_GATE.
STOCH_GATE_MODE = os.environ.get("DB_STOCH_GATE", "deeper")
STOCH_DEEPER_TOL = float(os.environ.get("DB_STOCH_DEEPER_TOL", "5"))
STOCH_TROUGH_MAX = 30.0
STOCH_ENTRY_MAX = 55.0
STOCH_W_LOOKBACK = 30
STOCH_W_PEAK_MIN = 60.0


def _stoch_second_bottom(df: pd.DataFrame) -> bool:
    """True if the stochastic permits entry under STOCH_GATE_MODE."""
    if STOCH_GATE_MODE == "off":
        return True
    k = df.get("stoch_k_fast")
    if k is None or len(k) < 6 or k.iloc[-4:].isna().any():
        return False
    k_now = float(k.iloc[-1])
    trough = float(k.iloc[-4:-1].min())
    rising = k_now > float(k.iloc[-2])
    if not (rising and trough <= STOCH_TROUGH_MAX and k_now <= STOCH_ENTRY_MAX):
        return False
    if STOCH_GATE_MODE == "turn":
        return True
    if STOCH_GATE_MODE == "deeper":
        hist = k.iloc[-(STOCH_W_LOOKBACK + 4):-4].dropna()
        if len(hist) < 5:
            return True   # no prior history to compare against
        prev_trough = float(hist.min())
        return trough <= prev_trough + STOCH_DEEPER_TOL
    # "w": walk back from the current trough — first find a peak, then an
    # earlier trough below it
    hist = k.iloc[-(STOCH_W_LOOKBACK + 4):-4].dropna().values
    seen_peak = False
    for v in reversed(hist):
        if not seen_peak:
            if v >= STOCH_W_PEAK_MIN:
                seen_peak = True
        elif v <= STOCH_TROUGH_MAX:
            return True
    return False


# Penny bottoms double-print the exact same low; a strict pivot test rejects
# them. A neighbor within 0.1% of the pivot low counts as "not lower".
PIVOT_TIE_PCT = 0.001
# A higher second low is a healthy W (LUCY 2026-07-06: 1.09 vs 1.05).
# Low2 may sit above Low1 by up to max(2%, 2 ATR); below-Low1 tolerance
# stays tight (LOW_TOLERANCE_PCT / LOW_ATR_TOLERANCE).
LOW2_ABOVE_PCT = 0.02
LOW2_ABOVE_ATR = 2.0

# Resistance-capped target (added 2026-07-08, VTAK): if a 2+ touch swing-high
# level sits between entry and the measured-move target, the realistic upside
# ends there — cap the target just under it and SELL there (take-profit,
# unlike normal targets which are advisory and exits ride the trail).
RES_CAP_BUFFER = 0.997           # sell a hair below the wall, don't need the tag
RES_CAP_MIN_REWARD_R = 0.5       # if the capped reward is under 0.5R, no trade


def _cap_target_at_resistance(df, close: float, stop: float, target: float,
                              atr: float = 0.0):
    """Return (target, take_profit, room_ok)."""
    res = find_overhead_resistance(df, close, atr)
    if res is None or res * RES_CAP_BUFFER >= target:
        return target, False, True
    capped = res * RES_CAP_BUFFER
    if capped - close < RES_CAP_MIN_REWARD_R * (close - stop):
        return capped, True, False
    return capped, True, True


def _is_swing_low(lows, idx: int) -> bool:
    if idx < PIVOT or idx > len(lows) - PIVOT_RIGHT - 1:
        return False
    pivot = lows[idx]
    floor = pivot * (1 - PIVOT_TIE_PCT)   # equal/near-equal lows don't disqualify
    for k in range(1, PIVOT + 1):
        if lows[idx - k] < floor:
            return False
    for k in range(1, PIVOT_RIGHT + 1):
        if lows[idx + k] < floor:
            return False
    return True


def _had_downtrend(closes, idx: int) -> bool:
    if idx < DOWNTREND_LOOKBACK:
        return False
    return closes[idx - DOWNTREND_LOOKBACK] > closes[idx]


def _find_w_pattern(df: pd.DataFrame, atr: float) -> Optional[Tuple[float, float, float, int]]:
    """Return (low1, low2, neckline, low2_idx) or None."""
    if len(df) < MIN_BARS:
        return None

    window = df.iloc[-WINDOW:].reset_index(drop=True)
    lows = window["low"].values
    highs = window["high"].values
    closes = window["close"].values
    n = len(lows)

    pivots = [i for i in range(n) if _is_swing_low(lows, i)]
    if len(pivots) < 2:
        return None

    # Prefer the most recent valid pattern (latest Low2 first)
    for i2 in reversed(pivots):
        l2 = lows[i2]
        c2 = closes[i2]
        for i1 in pivots:
            gap = i2 - i1
            if gap < MIN_GAP or gap > MAX_GAP:
                continue
            l1 = lows[i1]
            if not _had_downtrend(closes, i1):
                continue
            if l2 >= l1:   # higher low: bullish, generous tolerance
                if l2 - l1 > max(LOW2_ABOVE_PCT * l1, LOW2_ABOVE_ATR * atr):
                    continue
            else:          # lower low: pattern is failing, tight tolerance
                if l1 - l2 > max(LOW_TOLERANCE_PCT * l1, LOW_ATR_TOLERANCE * atr):
                    continue
            if c2 < l1 * (1 - LOW2_UNDERSHOOT_PCT):
                continue
            neckline = float(highs[i1:i2 + 1].max())
            bounce = neckline - l1
            min_bounce = max(MIN_BOUNCE_PCT * l1, MIN_BOUNCE_ATR * atr)
            if bounce < min_bounce:
                continue
            if MAX_BOUNCE_PCT > 0 and bounce > MAX_BOUNCE_PCT * l1:
                continue
            return (float(l1), float(l2), neckline, i2)

    return None


class DoubleBottom(BaseStrategy):
    name = "double_bottom"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        if len(df) < MIN_BARS:
            return Signal(ticker, self.name, 0, notes="insufficient_bars")

        row = df.iloc[-1]
        close = float(row["close"])
        atr = row.get("atr")
        if not atr or atr <= 0:
            return Signal(ticker, self.name, 0, entry_price=close, notes="no_atr")

        result = _find_w_pattern(df, float(atr))
        if result is None:
            return Signal(ticker, self.name, 0, entry_price=close, notes="no_w_pattern")

        l1, l2, neckline, i2 = result
        stop = l2 - STOP_ATR_MULT * float(atr)
        stoch_ok = _stoch_second_bottom(df)

        # ── Mode 1: EARLY entry at the second bottom ──────────────────────
        # Low2's pivot just confirmed and price is bouncing off it but has
        # not reached the neckline yet — buy the bottom, not the breakout.
        if close < neckline * BREAKOUT_BUFFER:
            bars_since_low2 = (min(len(df), WINDOW) - 1) - i2
            is_green = close > float(row["open"])
            bounced = close >= l2 + EARLY_CONFIRM_ATR * float(atr)
            fresh = bars_since_low2 <= PIVOT_RIGHT + EARLY_MAX_BARS_AFTER_LOW2
            # still near the bottom — past halfway to the neckline the
            # R:R is gone and this is no longer an "early" entry
            still_early = close <= l2 + EARLY_MAX_RETRACE * (neckline - l2)
            if is_green and bounced and fresh and still_early:
                if not stoch_ok:
                    return Signal(
                        ticker, self.name, 0, entry_price=close,
                        notes=f"w_stoch_gate(early,neck={neckline:.3f})",
                    )
                target, tp, room = _cap_target_at_resistance(
                    df, close, stop, neckline + (neckline - l1), float(atr))
                if not room:
                    return Signal(
                        ticker, self.name, 0, entry_price=close,
                        notes=f"w_no_room(early,res_target={target:.3f})",
                    )
                return Signal(
                    ticker=ticker,
                    strategy=self.name,
                    score=5,
                    entry_price=close,
                    notes=(
                        f"w_early(low1={l1:.3f},low2={l2:.3f},"
                        f"neck={neckline:.3f},bars={bars_since_low2})"
                    ),
                    stop_price=stop,
                    target_price=target,
                    take_profit=tp,
                    ignore_vwap=True,
                )

            # ── Mode 1b: PULLBACK RETEST after a confirmed neckline break ──
            # The neckline already broke (close above it within the last
            # RETEST_MAX_BARS), price dipped back below it but held the
            # upper half of the W, and this bar is bouncing off the dip.
            window = df.iloc[-WINDOW:]
            closes = window["close"].values
            lows = window["low"].values
            n = len(closes)
            break_idx = next(
                (j for j in range(i2 + 1, n - 1)
                 if closes[j] > neckline * BREAKOUT_BUFFER),
                None,
            )
            if (break_idx is not None
                    and (n - 1) - break_idx <= RETEST_MAX_BARS
                    and break_idx + 1 < n):
                pb_low = float(lows[break_idx + 1:].min())
                dipped = pb_low < neckline
                held = pb_low >= l2 + RETEST_MIN_HOLD * (neckline - l2)
                bounced_rt = close >= pb_low + EARLY_CONFIRM_ATR * float(atr)
                if is_green and dipped and held and bounced_rt:
                    if not stoch_ok:
                        return Signal(
                            ticker, self.name, 0, entry_price=close,
                            notes=f"w_stoch_gate(retest,neck={neckline:.3f})",
                        )
                    rt_stop = pb_low - STOP_ATR_MULT * float(atr)
                    target, tp, room = _cap_target_at_resistance(
                        df, close, rt_stop, neckline + (neckline - l1), float(atr))
                    if not room:
                        return Signal(
                            ticker, self.name, 0, entry_price=close,
                            notes=f"w_no_room(retest,res_target={target:.3f})",
                        )
                    return Signal(
                        ticker=ticker,
                        strategy=self.name,
                        score=5,
                        entry_price=close,
                        notes=(
                            f"w_retest(neck={neckline:.3f},pb_low={pb_low:.3f},"
                            f"bars={(n - 1) - break_idx})"
                        ),
                        stop_price=rt_stop,
                        target_price=target,
                        take_profit=tp,
                    )

            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"w_pending(low1={l1:.3f},low2={l2:.3f},neck={neckline:.3f})",
            )
        if close > neckline * MAX_ENTRY_EXTENSION:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"w_extended(neck={neckline:.3f},close={close:.3f})",
            )

        # Entry gate 2: volume confirmation
        avg_vol = row.get("avg_vol")
        vol = float(row.get("volume", 0) or 0)
        if not pd.notna(avg_vol) or avg_vol <= 0 or vol < avg_vol:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"w_no_vol_confirm(vol={vol:.0f},avg={avg_vol})",
            )

        # Pattern-specific target (stop computed above)
        if not stoch_ok:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"w_stoch_gate(breakout,neck={neckline:.3f})",
            )

        target, tp, room = _cap_target_at_resistance(
            df, close, stop, close + (neckline - l1), float(atr))
        if not room:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=f"w_no_room(breakout,res_target={target:.3f})",
            )

        return Signal(
            ticker=ticker,
            strategy=self.name,
            score=5,
            entry_price=close,
            notes=(
                f"w_breakout(low1={l1:.3f},low2={l2:.3f},neck={neckline:.3f},"
                f"vol={vol/avg_vol:.1f}x)"
            ),
            stop_price=stop,
            target_price=target,
            take_profit=tp,
        )
