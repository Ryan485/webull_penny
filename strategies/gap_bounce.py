"""
Strategy 1: Morning Gap Bounce (9:30–10:30 AM only)
Stock gapped down ≥5%, sold off more, now bouncing.
"""
from typing import Optional
import pandas as pd

from strategies.base import BaseStrategy, Signal
from data.indicators import latest, prev


class GapBounce(BaseStrategy):
    name = "gap_bounce"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        row = latest(df)
        score = 0
        notes = []

        # 1. Gap down ≥5% from prior close
        if prior_close and prior_close > 0:
            gap_pct = (row["open"] - prior_close) / prior_close
            if gap_pct <= -0.05:
                score += 1
                notes.append(f"gap_down={gap_pct:.1%}")

        # 2. RSI ≤ 35
        if pd.notna(row.get("rsi")) and row["rsi"] <= 35:
            score += 1
            notes.append(f"rsi={row['rsi']:.1f}")

        # 3. Stochastic fast %K crosses above %D from below 30
        if len(df) >= 2:
            prev_row = prev(df)
            k_now = row.get("stoch_k_fast")
            d_now = row.get("stoch_d_fast")
            k_prev = prev_row.get("stoch_k_fast")
            d_prev = prev_row.get("stoch_d_fast")
            if (pd.notna(k_now) and pd.notna(d_now) and
                    pd.notna(k_prev) and pd.notna(d_prev)):
                cross_up = k_prev <= d_prev and k_now > d_now
                if cross_up and k_now < 30:
                    score += 1
                    notes.append(f"stoch_cross_up(k={k_now:.1f})")

        # 4. Volume spike ≥ 2× average
        avg_vol = row.get("avg_vol")
        if pd.notna(avg_vol) and avg_vol > 0 and row["volume"] >= 2 * avg_vol:
            score += 1
            notes.append(f"vol_spike={row['volume']/avg_vol:.1f}x")

        # 5. VWAP reclaim: close > VWAP after open was below VWAP
        vwap = row.get("vwap")
        if pd.notna(vwap):
            if row["open"] < vwap and row["close"] > vwap:
                score += 1
                notes.append("vwap_reclaim")

        return Signal(
            ticker=ticker,
            strategy=self.name,
            score=score,
            entry_price=row["close"],
            notes=", ".join(notes),
        )
