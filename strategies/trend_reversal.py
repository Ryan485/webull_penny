"""
Strategy 3: Trend Reversal — Downtrend → Flat → Up (all day)
Was falling, consolidated sideways, now turning up.
"""
from typing import Optional
import pandas as pd

from strategies.base import BaseStrategy, Signal
from data.indicators import latest, prev


class TrendReversal(BaseStrategy):
    name = "trend_reversal"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        row = latest(df)
        score = 0
        notes = []

        if len(df) < 60:
            return Signal(ticker=ticker, strategy=self.name, score=0)

        prev_row = prev(df)

        # 1. Prior downtrend: MA5 < MA10 for majority of candles leading up
        # to the day's low. HARD GATE since 2026-07-14 (external review):
        # this is the defining structure of the strategy. As a mere +1 it
        # could be skipped entirely — the remaining components (MA cross,
        # stoch cross, RSI recovery) are three correlated echoes of the same
        # bounce and could reach the trigger score with no reversal setup.
        low_idx = df["low"].idxmin()
        before_low = df.loc[:low_idx].dropna(subset=["ma5", "ma10"])
        if len(before_low) < 10:
            return Signal(ticker=ticker, strategy=self.name, score=0,
                          notes="no_prior_downtrend(short)")
        bearish_candles = (before_low["ma5"] < before_low["ma10"]).sum()
        if bearish_candles / len(before_low) < 0.6:
            return Signal(ticker=ticker, strategy=self.name, score=0,
                          notes="no_prior_downtrend")
        score += 1
        notes.append(f"prior_downtrend({bearish_candles}/{len(before_low)})")

        # 2. Flattening: find the day's low, check that candles since then are tight (≤ 2% range)
        low_idx = df["low"].idxmin()
        since_low = df.loc[low_idx:]
        if len(since_low) >= 5:
            rng = (since_low["high"].max() - since_low["low"].min()) / since_low["close"].mean()
            if rng <= 0.02:
                score += 1
                notes.append(f"flat_since_low={rng:.1%}({len(since_low)}bars)")

        # 3. MA5 crosses above MA10 on current candle
        ma5_now = row.get("ma5")
        ma10_now = row.get("ma10")
        ma5_prev = prev_row.get("ma5")
        ma10_prev = prev_row.get("ma10")
        if (pd.notna(ma5_now) and pd.notna(ma10_now)
                and pd.notna(ma5_prev) and pd.notna(ma10_prev)):
            if ma5_prev <= ma10_prev and ma5_now > ma10_now:
                score += 1
                notes.append("ma_crossover")

        # 4. Stochastic medium %K crosses above %D, both below 60
        k = row.get("stoch_k_med")
        d = row.get("stoch_d_med")
        kp = prev_row.get("stoch_k_med")
        dp = prev_row.get("stoch_d_med")
        if (pd.notna(k) and pd.notna(d) and pd.notna(kp) and pd.notna(dp)):
            if kp <= dp and k > d and k < 60 and d < 60:
                score += 1
                notes.append(f"stoch_bullish(k={k:.1f})")

        # 5. RSI hit oversold (<35) at any point today and has now recovered to ≥40
        rsi_all = df["rsi"].dropna()
        rsi_now = row.get("rsi")
        if len(rsi_all) >= 10 and pd.notna(rsi_now):
            rsi_min = rsi_all.min()
            if rsi_min < 35 and rsi_now >= 40:
                score += 1
                notes.append(f"rsi_recovery(min={rsi_min:.0f}→now={rsi_now:.0f})")

        return Signal(
            ticker=ticker,
            strategy=self.name,
            score=score,
            entry_price=row["close"],
            notes=", ".join(notes),
        )
