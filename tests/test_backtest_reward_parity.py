"""Sim/live parity for the capped-target reward-floor guard.

main.py revalidates a capped signal's reward AFTER the v1.6 MIN_STOP_PCT
noise floor may have widened its stop (see tests/test_reward_quote_drift.py
for the live-quote half of this). backtest_viral.py applies the same stop
floor but, before this fix, never re-checked reward against it -- so the sim
could open a trade live would now reject, silently diverging (Codex
NEW-CAPPED-REWARD-BACKTEST-PARITY, 2026-08-04).

Reproduces Codex's own example: entry 1.00, structural stop 0.99 (1% away,
inside the 3% noise floor), target 1.012 -> passes the strategy's own 0.5R
floor at 1.2R, but the floor widens the stop to 0.97, dropping the trade to
0.4R -- below the floor. simulate_day() must produce ZERO trades.

Run: py -3.12 -m pytest tests -q
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pandas as pd
import pytest
import pytz

import backtest_viral as bt
import config
from strategies.base import Signal

ET = pytz.timezone("America/New_York")

ENTRY, STRUCTURAL_STOP, TARGET = 1.00, 0.99, 1.012
MIN_REWARD_R = 0.5   # matches double_bottom/resistance_breakout's RES_CAP_MIN_REWARD_R


def _make_day_df(n=60):
    idx = pd.date_range("2026-08-05 10:05", periods=n, freq="1min", tz=ET)
    return pd.DataFrame({
        "open": [ENTRY] * n, "high": [ENTRY] * n, "low": [ENTRY] * n,
        "close": [ENTRY] * n, "volume": [10_000] * n,
        "atr": [0.02] * n, "vwap": [float("nan")] * n,
    }, index=idx)


class _FixedSignalStrategy:
    name = "double_bottom"

    def __init__(self, min_reward_r):
        self._min_reward_r = min_reward_r

    def evaluate(self, ticker, window, prior_close=None):
        return Signal(
            ticker=ticker, strategy=self.name, score=5, entry_price=ENTRY,
            stop_price=STRUCTURAL_STOP, target_price=TARGET,
            take_profit=True, min_reward_r=self._min_reward_r,
        )


def test_backtest_skips_a_trade_that_only_fails_its_floor_after_stop_widening(monkeypatch):
    monkeypatch.setattr(bt, "STRATEGIES", [_FixedSignalStrategy(MIN_REWARD_R)])
    df = _make_day_df()

    # Sanity check the fixture reproduces Codex's exact numbers before
    # trusting the "zero trades" assertion below.
    risk_at_close = ENTRY - STRUCTURAL_STOP
    reward_at_close = TARGET - ENTRY
    assert reward_at_close / risk_at_close == pytest.approx(1.2, abs=0.01)
    floor_stop = ENTRY * (1 - config.MIN_STOP_PCT)
    risk_after_floor = ENTRY - floor_stop
    assert reward_at_close / risk_after_floor == pytest.approx(0.4, abs=0.01)

    trades = []
    bt.simulate_day("TEST", "2026-08-05", df, trades)
    assert trades == []


def test_backtest_still_takes_the_trade_without_the_stop_widening_penalty(monkeypatch):
    """Control: the same signal with a floor loose enough to survive the
    widened stop (0.35R) must still produce a trade, proving the zero-trades
    result above is the new guard firing, not the harness being broken."""
    monkeypatch.setattr(bt, "STRATEGIES", [_FixedSignalStrategy(0.35)])
    df = _make_day_df()
    trades = []
    bt.simulate_day("TEST", "2026-08-05", df, trades)
    assert len(trades) == 1
