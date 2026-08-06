"""
Strategy 3: Trend Reversal — structural bullish reversal (all day)

REWRITTEN 2026-07-14 (owner directive, VMAR): the old version scored
indicator echoes (MA cross + stoch cross + RSI recovery) and bought the
fade of a parabolic spike with no structure (VMAR 10:00, -10.5%). The
owner wants the classic bullish-reversal patterns chased by their
NECKLINE: inverse head & shoulders, rounding bottom, cup & handle.
On a 1-minute chart they all share one skeleton:

    prior downtrend -> base low (head / cup bottom)
    -> rally high(s) after the low  = NECKLINE (tracked resistance)
    -> HIGHER low on the right side (right shoulder / handle)
    -> entry when price breaks the neckline, or retests it after a break

Triple bottom / rectangle is deliberately NOT here: double_bottom fires
on its last two lows and resistance_breakout on the box top. Bullish
wedge is excluded per owner (2026-07-14).

Rules (1-minute candles, rolling 120-bar window):
  - Prior downtrend into the base low is a HARD GATE (MA5 < MA10 for
    >=60% of bars before the low) — kept from the 2026-07-14 review.
  - Right-side low = most recent tie-tolerant swing low (5-left/3-right)
    at least 5 bars after the base low, strictly above it, below the
    neckline. Neckline = max high between base low and right low.
  - Depth (neckline - base low) must be >= max(4%, 3 ATR) and <= 20% of
    the base low — same parabolic-spike cap as double_bottom (a 24-52%
    "base" is a collapse being misread, not a reversal; SOBR/JZXN).
  - BREAKOUT entry: close in [neck*1.002, neck*1.02], within 3 bars of
    the first close above the neckline after the right low confirmed.
    Volume gate is on the ENTRY candle (>= 1.5x average) — deliberately
    the same convention as resistance_breakout: what matters is
    participation at the fill, not at a candle we did not trade.
    Stop: right low - 0.5 ATR.
  - RETEST entry: first break 4-30 bars old, price pulled back below the
    neckline while holding the UPPER HALF between the right low and the
    neckline, current bar green and bouncing >= 0.5 ATR off the pullback
    low. Stop: pullback low - 0.5 ATR. (Same convention as
    double_bottom/resistance_breakout.)
  - Target: neckline + (neckline - base low), capped at overhead
    resistance (real take-profit there); capped reward < 0.5R = no trade.
"""
from typing import List, Optional

import pandas as pd

from strategies.base import BaseStrategy, Signal, build_capped_signal
from strategies.double_bottom import _cap_target_at_resistance


WINDOW = 120
MIN_BARS = 45                    # same premarket-poor-ticker rule as the others
PIVOT = 5                        # bars higher LEFT of a swing low
PIVOT_RIGHT = 3                  # confirmation bars RIGHT of the low
PIVOT_TIE_PCT = 0.001            # penny lows double-print — ties don't disqualify
MIN_BASE_GAP = 5                 # right low must form >=5 bars after the base low
MIN_DEPTH_PCT = 0.04             # base must have real depth: max(4%, 3 ATR)
MIN_DEPTH_ATR = 3.0
MAX_DEPTH_PCT = 0.20             # ...but a >20% "base" is a collapse (SOBR rule)
BREAKOUT_BUFFER = 1.002          # close must clear the neckline by 0.2%
MAX_ENTRY_EXTENSION = 1.02       # ...but not more than 2% above (don't chase)
FRESH_BREAK_BARS = 3             # may enter up to 3 bars after the first break
MIN_VOL_MULT = 1.5               # breakout volume >= 1.5x average
STOP_ATR_MULT = 0.5
RETEST_MAX_BARS = 30             # a break older than this is spent
RETEST_CONFIRM_ATR = 0.5         # bounce off the pullback low must be >= 0.5 ATR
RETEST_MIN_HOLD = 0.5            # pullback low must hold the upper half between
                                 # the right low and the neckline — one cent above
                                 # the right low is a failing breakout, not a
                                 # retest (same convention as double_bottom;
                                 # external review 2026-07-14)
DOWNTREND_MIN_BARS = 10          # need history before the low to judge the trend
DOWNTREND_MIN_FRAC = 0.6         # MA5 < MA10 on >=60% of bars before the low


def _swing_lows(lows) -> List[int]:
    out = []
    for i in range(PIVOT, len(lows) - PIVOT_RIGHT):
        pivot = lows[i]
        floor = pivot * (1 - PIVOT_TIE_PCT)
        if all(lows[i - k] >= floor for k in range(1, PIVOT + 1)) \
                and all(lows[i + k] >= floor for k in range(1, PIVOT_RIGHT + 1)):
            out.append(i)
    return out


class TrendReversal(BaseStrategy):
    name = "trend_reversal"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        if len(df) < MIN_BARS:
            return Signal(ticker, self.name, 0, notes="insufficient_bars")

        row = df.iloc[-1]
        close = float(row["close"])
        atr = row.get("atr")
        if not atr or atr <= 0:
            return Signal(ticker, self.name, 0, entry_price=close, notes="no_atr")
        atr = float(atr)

        window = df.iloc[-WINDOW:]
        lows = window["low"].values
        highs = window["high"].values
        closes = window["close"].values
        opens = window["open"].values
        n = len(lows)

        # ── Base low (head / cup bottom) = the window low ─────────────────
        i_head = int(lows.argmin())
        head_low = float(lows[i_head])

        # HARD GATE: prior downtrend into the base low, judged on the full
        # history before it (not just the window). Without a real decline
        # there is nothing to reverse — the remaining structure would just
        # be a consolidation inside an uptrend.
        before_head = df.iloc[:len(df) - n + i_head].dropna(subset=["ma5", "ma10"])
        if len(before_head) < DOWNTREND_MIN_BARS:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="no_prior_downtrend(short)")
        bearish = int((before_head["ma5"] < before_head["ma10"]).sum())
        if bearish / len(before_head) < DOWNTREND_MIN_FRAC:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="no_prior_downtrend")

        # ── Right-side higher low (right shoulder / handle) ───────────────
        pivots = [i for i in _swing_lows(lows)
                  if i >= i_head + MIN_BASE_GAP and lows[i] > head_low]
        if not pivots:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="rev_no_right_low")

        # Most recent valid structure first
        for i_rs in reversed(pivots):
            rs_low = float(lows[i_rs])

            # Neckline = the rally high between base low and right low —
            # the tracked resistance the owner wants the entry keyed to.
            neckline = float(highs[i_head:i_rs + 1].max())
            if rs_low >= neckline:
                continue

            depth = neckline - head_low
            if depth < max(MIN_DEPTH_PCT * head_low, MIN_DEPTH_ATR * atr):
                continue   # too shallow to be a tradeable base
            if depth > MAX_DEPTH_PCT * head_low:
                continue   # parabolic collapse misread as a base (SOBR rule)

            # First close above the neckline AFTER the right low confirmed
            first_break = next(
                (j for j in range(i_rs + 1, n - 1)
                 if closes[j] > neckline * BREAKOUT_BUFFER),
                None,
            )
            bars_since_break = None if first_break is None \
                else (n - 1) - first_break
            if bars_since_break is not None and bars_since_break > RETEST_MAX_BARS:
                continue   # break is spent

            stop = rs_low - STOP_ATR_MULT * atr

            # ── BREAKOUT entry: this close is the break (or within 3 bars)
            fresh = bars_since_break is None or bars_since_break <= FRESH_BREAK_BARS
            if fresh and neckline * BREAKOUT_BUFFER <= close <= neckline * MAX_ENTRY_EXTENSION:
                avg_vol = row.get("avg_vol")
                vol = float(row.get("volume", 0) or 0)
                if not pd.notna(avg_vol) or avg_vol <= 0 \
                        or vol < MIN_VOL_MULT * avg_vol:
                    return Signal(
                        ticker, self.name, 0, entry_price=close,
                        notes=f"rev_no_vol_confirm(neck={neckline:.3f})",
                    )
                cap_result = _cap_target_at_resistance(
                    df, close, stop, neckline + depth, atr)
                if not cap_result[2]:   # room_ok
                    return Signal(
                        ticker, self.name, 0, entry_price=close,
                        notes=f"rev_no_room(res_target={cap_result[0]:.3f})",
                    )
                return build_capped_signal(
                    ticker=ticker,
                    strategy=self.name,
                    score=5,
                    entry_price=close,
                    notes=(
                        f"rev_breakout(head={head_low:.3f},rs={rs_low:.3f},"
                        f"neck={neckline:.3f},vol={vol/avg_vol:.1f}x)"
                    ),
                    stop_price=stop,
                    cap_result=cap_result,
                )

            # ── RETEST entry: broke earlier, pulled back to the neckline
            if bars_since_break is not None and bars_since_break > FRESH_BREAK_BARS:
                pb_low = float(lows[first_break + 1:].min())
                is_green = close > float(opens[-1])
                dipped = pb_low < neckline
                held = pb_low >= rs_low + RETEST_MIN_HOLD * (neckline - rs_low)
                bounced = close >= pb_low + RETEST_CONFIRM_ATR * atr
                not_extended = close <= neckline * MAX_ENTRY_EXTENSION
                if is_green and dipped and held and bounced and not_extended:
                    rt_stop = pb_low - STOP_ATR_MULT * atr
                    cap_result = _cap_target_at_resistance(
                        df, close, rt_stop, neckline + depth, atr)
                    if not cap_result[2]:   # room_ok
                        return Signal(
                            ticker, self.name, 0, entry_price=close,
                            notes=f"rev_no_room(retest,res_target={cap_result[0]:.3f})",
                        )
                    return build_capped_signal(
                        ticker=ticker,
                        strategy=self.name,
                        score=5,
                        entry_price=close,
                        notes=(
                            f"rev_retest(neck={neckline:.3f},"
                            f"pb_low={pb_low:.3f},bars={bars_since_break})"
                        ),
                        stop_price=rt_stop,
                        cap_result=cap_result,
                    )

        return Signal(ticker, self.name, 0, entry_price=close,
                      notes="rev_pending")
