from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class Signal:
    ticker: str
    strategy: str
    score: int
    max_score: int = 5
    entry_price: float = 0.0
    notes: str = ""
    # Optional strategy-specific overrides — if None, main loop uses ATR-based math.
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    # Early mean-reversion entries (e.g. W second bottom) are naturally below
    # session VWAP; setting this lets the signal bypass the VWAP regime gate.
    ignore_vwap: bool = False
    # True when target_price was capped at overhead resistance: sell AT the
    # target instead of letting the trail decide (normal targets are advisory).
    take_profit: bool = False

    @property
    def triggered(self) -> bool:
        from config import SCORE_THRESHOLD
        return self.score >= SCORE_THRESHOLD


class BaseStrategy:
    name: str = "base"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        raise NotImplementedError
