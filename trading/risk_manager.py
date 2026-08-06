"""
Risk management: position sizing, stop/target calculation, daily halt.
"""
import math
from typing import Tuple
import pandas as pd

import config
from data.indicators import latest


def calc_stop_and_target(entry: float, atr: float) -> Tuple[float, float]:
    """
    Stop: ATR(14) × 1.5 below entry, capped at 5% below entry.
    Target: max(2R, entry × 1.10).
    Returns (stop_price, target_price).
    """
    atr_stop = entry - atr * config.ATR_STOP_MULT
    hard_stop = entry * (1 - config.MAX_STOP_PCT)
    stop = max(atr_stop, hard_stop)       # tighter of the two

    risk = entry - stop
    target_2r = entry + risk * config.TAKE_PROFIT_R
    target_10pct = entry * (1 + config.TAKE_PROFIT_MIN_PCT)
    target = max(target_2r, target_10pct)

    return round(stop, 4), round(target, 4)


def stop_is_valid(entry: float, stop: float) -> bool:
    """A usable protective stop is finite, positive, and strictly below entry.

    NaN-safe on purpose: `stop < entry` is False for NaN, so NaN is rejected here
    rather than slipping through a comparison-based clamp elsewhere.
    """
    if not (math.isfinite(entry) and math.isfinite(stop)):
        return False
    return 0.0 < stop < entry


def calc_position_size(account_value: float, entry: float, stop: float) -> int:
    """
    Risk 2% of account per trade.
    shares = (account × risk_pct) / (entry − stop)
    Capped so total position cost ≤ 20% of account.

    Returns 0 for an unusable stop. This used to FAIL OPEN: risk-per-share was
    floored at $0.01 and the result at 1 share, so a stop at or above entry (or a
    NaN one) still produced a tradeable quantity and main.py would place the buy
    (Codex R1-INVALID-STOP). Both callers already skip on `shares < 1`.
    """
    if not (math.isfinite(account_value) and account_value > 0):
        return 0
    if not stop_is_valid(entry, stop):
        return 0
    risk_dollars = account_value * config.RISK_PCT_PER_TRADE
    risk_per_share = max(entry - stop, 0.01)
    shares = math.floor(risk_dollars / risk_per_share)
    max_shares = math.floor(account_value * 0.20 / entry)
    shares = min(shares, max_shares)
    return max(shares, 1)


def daily_halt_triggered(daily_pnl: float, account_value: float) -> bool:
    """Bot stops trading if daily P&L drops below −6% of account."""
    return daily_pnl < -(account_value * config.DAILY_HALT_PCT)
