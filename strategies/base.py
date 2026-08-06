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
    # The minimum reward/risk this signal was validated against AT THE CANDLE
    # CLOSE, when target_price is a FIXED price (take_profit=True) rather than
    # a multiple of risk at the entry. A fixed target does not move with the
    # entry, so a live quote that has drifted (still inside the chase guard's
    # allowance) can silently erode the reward below what was validated —
    # main.py revalidates this against the executable quote before submitting
    # (Codex NEW-CAPPED-TARGET-QUOTE-DRIFT, 2026-08-04). None means "no fixed
    # floor to revalidate" (e.g. take_profit=False, or an unlimited/advisory
    # target that scales with entry and so cannot drift out of tolerance).
    min_reward_r: Optional[float] = None

    @property
    def triggered(self) -> bool:
        from config import SCORE_THRESHOLD
        return self.score >= SCORE_THRESHOLD


def build_capped_signal(ticker: str, strategy: str, score: int,
                         entry_price: float, notes: str, stop_price: float,
                         cap_result, **extra) -> Signal:
    """The ONLY place a resistance-capped Signal is constructed.

    cap_result is the (target, take_profit, room_ok, min_reward_r) tuple
    returned by double_bottom._cap_target_at_resistance or
    resistance_breakout._overhead_cap. Codex NEW-CAPPED-TARGET-QUOTE-DRIFT
    (round 6, 2026-08-05): returning min_reward_r from those two functions
    (round 5's fix) stopped a call site from referencing the WRONG value, but
    a call site could still correctly unpack it and then simply not pass
    min_reward_r=... into its own Signal(...) call -- an omission, not a
    mismatch, and nothing about a 4-way tuple unpack prevents that. Routing
    every one of the 7 capped-target call sites (double_bottom.py x3,
    trend_reversal.py x2, resistance_breakout.py x2) through this single
    function instead of constructing Signal(...) directly makes the omission
    itself impossible: there is exactly one line of code in the whole
    codebase that sets take_profit/min_reward_r/target_price from a cap
    result, and every capped-target signal is required to pass through it.

    Caller must have already handled `if not room_ok: ...` (typically a
    downgraded or empty Signal) before calling this -- it does not re-check
    room_ok itself, since the caller's own "no room" Signal often differs
    (score=0, different notes) and duplicating that here would be worse than
    letting each caller keep its own no-room branch, which is orthogonal to
    what THIS function centralizes.
    """
    target, take_profit, room_ok, min_reward_r = cap_result
    return Signal(
        ticker=ticker, strategy=strategy, score=score,
        entry_price=entry_price, notes=notes, stop_price=stop_price,
        target_price=target, take_profit=take_profit,
        min_reward_r=min_reward_r, **extra,
    )


class BaseStrategy:
    name: str = "base"

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        raise NotImplementedError
