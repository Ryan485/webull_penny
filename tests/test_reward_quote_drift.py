"""Behavioral (not just arithmetic) regression for the capped-target reward
revalidation guard in main.check_entries().

Codex round 2 (2026-08-04) rejected arithmetic-only + source-text evidence for
this finding: the requirement is to prove main.check_entries() itself skips
the broker call when a signal's reward has drifted below its floor by the
time of the executable quote, and that it does NOT skip a signal that never
drifted. Round 3 rejected the round-2 fix again: it proved the mechanism for
box_range only, using a hand-fabricated Signal, so "changing or omitting
min_reward_r in any real strategy producer would not fail these tests".

Two groups of tests below:
  * box_range: a fabricated-but-representative Signal (already accepted by
    Codex round 4 as resolving NEW-BOX-REWARD-QUOTE-DRIFT specifically for
    box_range -- left as-is, not touched this round).
  * double_bottom / trend_reversal / resistance_breakout: each strategy's
    REAL evaluate() is called on a genuinely triggering synthetic price
    series -- not a fabricated Signal -- so a future change that drops
    min_reward_r from any real producer's Signal(...) call site would make
    these tests fail. The resulting REAL Signal is then fed through
    main.check_entries() exactly as the box_range test does.

Run: py -3.12 -m pytest tests -q
"""
import os
import sys
from unittest.mock import MagicMock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import numpy as np
import pandas as pd
import pytest
import pytz

import main as bot
import strategies.box_range as box_range
import strategies.double_bottom as double_bottom
import strategies.resistance_breakout as resistance_breakout
import strategies.trend_reversal as trend_reversal
from data.indicators import compute_all
from strategies.base import Signal

ET = pytz.timezone("America/New_York")

# --- box_range: fabricated-signal coverage (unchanged from round 3) --------

CLOSE, STOP, TARGET = 1.005, 0.97485, 1.03688
DRIFTED_QUOTE = 1.020   # within MAX_ENTRY_CHASE_PCT (1.5%) of CLOSE -> not chase-blocked
NO_DRIFT_QUOTE = CLOSE  # fills exactly at the validated price


def _make_bars(n=60):
    return pd.DataFrame({
        "open": [CLOSE] * n, "high": [CLOSE] * n, "low": [CLOSE] * n,
        "close": [CLOSE] * n, "volume": [10_000] * n,
        "atr": [0.02] * n, "vwap": [float("nan")] * n,
    })


def _make_fabricated_signal():
    return Signal(
        ticker="TEST", strategy="box_range", score=5, entry_price=CLOSE,
        stop_price=STOP, target_price=TARGET,
        take_profit=True, min_reward_r=box_range.MIN_REWARD_R,
    )


class _FixedSignalStrategy:
    """Wraps a PRECOMPUTED Signal (real or fabricated) so check_entries() sees
    a deterministic result without re-running pattern detection on every bar
    of the scan loop -- the point under test is check_entries' OWN reward
    guard, not the strategy's pattern detector."""
    def __init__(self, name, signal):
        self.name = name
        self._signal = signal

    def evaluate(self, ticker, df, prior_close=None):
        return self._signal


def _run_check_entries(monkeypatch, live_quote, strategy_name, signal, bars):
    monkeypatch.setattr(bot, "STRATEGIES", [_FixedSignalStrategy(strategy_name, signal)])
    # Bypass the opening-whipsaw time gate regardless of wall-clock time.
    monkeypatch.setattr(bot, "ENTRY_START_HOUR", 0)
    monkeypatch.setattr(bot, "ENTRY_START_MIN", 0)
    monkeypatch.setattr(bot, "get_catalyst", lambda ticker: "known")
    monkeypatch.setattr(bot, "get_live_bars", lambda ticker: bars)
    monkeypatch.setattr(bot, "compute_all", lambda df: df)   # passthrough
    monkeypatch.setattr(bot, "get_prior_close", lambda ticker: None)

    state = bot.BotState()
    state.watchlist = ["TEST"]

    portfolio = MagicMock()
    portfolio.can_open_position.return_value = True

    broker = MagicMock()
    broker.get_quote.return_value = live_quote
    broker.get_account_value.return_value = 100_000.0
    broker.place_market_buy.return_value = live_quote

    bot.check_entries(state, portfolio, broker)
    return broker, portfolio


def test_box_range_order_is_skipped_when_reward_drifts_below_floor(monkeypatch):
    broker, _ = _run_check_entries(monkeypatch, DRIFTED_QUOTE, "box_range",
                                    _make_fabricated_signal(), _make_bars())
    broker.place_market_buy.assert_not_called()


def test_box_range_order_still_submits_when_the_quote_has_not_drifted(monkeypatch):
    broker, portfolio = _run_check_entries(monkeypatch, NO_DRIFT_QUOTE, "box_range",
                                            _make_fabricated_signal(), _make_bars())
    broker.place_market_buy.assert_called_once()


# --- NEW-MISSING-LIVE-QUOTE-FAILS-OPEN (Codex round 9, 2026-08-05) ---------
#
# `live_price = broker.get_quote(ticker) or best.entry_price` silently fell
# back to the stale signal-candle price whenever the broker quote was
# unavailable. That fallback made live_price == best.entry_price exactly,
# so the chase guard, noise floor, and reward-drift revalidation above all
# see ZERO drift and wave the order through -- even though the actual fill
# price is completely unverified. Fixed: check_entries() now requires a
# finite, positive broker quote and skips the entry entirely otherwise,
# never substituting the candle price.

@pytest.mark.parametrize("bad_quote", [None, float("nan"), 0.0, -1.0])
def test_order_is_skipped_when_the_broker_quote_is_unavailable(monkeypatch, bad_quote):
    broker, portfolio = _run_check_entries(
        monkeypatch, bad_quote, "box_range", _make_fabricated_signal(), _make_bars())
    broker.place_market_buy.assert_not_called()
    broker.get_account_value.assert_not_called()   # sizing must not even run
    portfolio.add_position.assert_not_called()


def test_order_still_submits_with_a_genuinely_valid_quote(monkeypatch):
    """Control: proves the guard above is what's firing, not a broken harness."""
    broker, portfolio = _run_check_entries(
        monkeypatch, NO_DRIFT_QUOTE, "box_range", _make_fabricated_signal(), _make_bars())
    broker.place_market_buy.assert_called_once()
    portfolio.add_position.assert_called_once()
    portfolio.add_position.assert_called_once()


# --- Real producers: double_bottom, trend_reversal, resistance_breakout ----
#
# Each builder returns a genuinely triggering OHLCV series (real ATR/volume-
# average computed via compute_all, not hand-set) such that the strategy's
# REAL evaluate() reaches its resistance-capped Signal(...) call site through
# its actual pattern-detection logic: a real downtrend, a real base/pivot
# structure, a real overhead resistance zone with 2+ genuine swing-high
# touches, and a real breakout bar with volume confirmation. Numbers were
# derived empirically against the real detector code, not reverse-engineered
# from its thresholds by inspection alone.

def _build_double_bottom_df():
    n = 90
    idx = pd.date_range("2026-08-05 10:00", periods=n, freq="1min", tz=ET)
    close = np.full(n, 1.00)
    close[0:21] = np.linspace(1.10, 1.00, 21)     # downtrend into Low1 (bar 20)
    close[21:31] = np.linspace(1.00, 1.05, 10)    # rally to the neckline
    close[31:51] = np.linspace(1.05, 1.00, 20)    # pull back to Low2 (bar 50)
    close[51:89] = np.linspace(1.00, 1.02, 38)    # chop below the neckline
    close[89] = 1.055                             # breakout bar

    high = close + 0.0015
    low = close - 0.0015
    low[20] = 1.00
    low[50] = 1.00                                # clean W-pattern pivot lows
    high[60] = 1.090
    high[75] = 1.090                              # overhead zone, isolated touches
    high[89] = 1.056

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.full(n, 5000)
    vol[89] = 20000

    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                        "close": close, "volume": vol}, index=idx)
    return compute_all(df)


def _build_trend_reversal_df():
    n = 100
    idx = pd.date_range("2026-08-05 10:00", periods=n, freq="1min", tz=ET)
    close = np.full(n, 1.00)
    close[0:31] = np.linspace(1.30, 1.00, 31)     # downtrend into the base low (bar 30)
    close[31:41] = np.linspace(1.00, 1.06, 10)    # rally to the neckline
    close[41:56] = np.linspace(1.06, 1.01, 15)    # pull back to the right-side low (bar 55)
    close[56:99] = np.linspace(1.01, 1.03, 43)    # chop below the neckline
    close[99] = 1.07                              # breakout bar

    high = close + 0.0015
    low = close - 0.0015
    low[30] = 0.9985                              # base low (window minimum)
    low[55] = 1.0085                              # right-side low, above the base
    high[65] = 1.11
    high[80] = 1.11                               # overhead zone, isolated touches
    high[99] = 1.072

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.full(n, 5000)
    vol[99] = 20000

    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                        "close": close, "volume": vol}, index=idx)
    return compute_all(df)


def _build_resistance_breakout_df():
    # Deterministic sawtooth noise gives a realistic (non-near-zero) ATR --
    # without it the structural stop (resistance - 0.5*ATR) sits well inside
    # the v1.6 MIN_STOP_PCT noise floor, the floor overrides it regardless of
    # quote drift, and the resulting risk swamps ANY achievable capped target
    # (the blind 2R target is itself derived from the same too-tight
    # structural risk) -- both the drift and no-drift cases would fail
    # identically, proving nothing about the guard. Overhead level (1.078)
    # chosen empirically so the capped reward clears the 0.5R floor at the
    # unchased quote but drops well under it at the permitted +1.4% drift.
    n = 90
    idx = pd.date_range("2026-08-05 10:00", periods=n, freq="1min", tz=ET)
    saw = 0.035 * np.sin(np.arange(n) * 1.3)
    close = 1.00 + saw
    high = close + 0.006
    low = close - 0.006
    # Overhead level ~1.078: two isolated high-wicks; closes stay flat so it
    # is never counted as "broken".
    high[8] = 1.078
    high[25] = 1.078
    # Traded level ~1.0525: two isolated high-wicks, likewise unbroken until
    # the final bar.
    high[40] = 1.0525
    high[60] = 1.0525
    close[89] = 1.055                             # breakout bar
    high[89] = 1.056

    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = np.full(n, 5000)
    vol[89] = 20000

    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                        "close": close, "volume": vol}, index=idx)
    return compute_all(df)


REAL_PRODUCERS = [
    ("double_bottom", _build_double_bottom_df, double_bottom.DoubleBottom,
     double_bottom.RES_CAP_MIN_REWARD_R),
    ("trend_reversal", _build_trend_reversal_df, trend_reversal.TrendReversal,
     double_bottom.RES_CAP_MIN_REWARD_R),  # shares double_bottom's capping helper
    ("resistance_breakout", _build_resistance_breakout_df, resistance_breakout.ResistanceBreakout,
     resistance_breakout.RES_CAP_MIN_REWARD_R),
]


def _real_signal(monkeypatch, name, build_df, strategy_cls):
    if name == "double_bottom":
        monkeypatch.setattr(double_bottom, "STOCH_GATE_MODE", "off")
    if name == "resistance_breakout":
        monkeypatch.setattr(resistance_breakout, "OVERHEAD_CAP", "cap")
    df = build_df()
    sig = strategy_cls().evaluate("TEST", df)
    return sig, df


@pytest.mark.parametrize("name,build_df,strategy_cls,expected_floor", REAL_PRODUCERS)
def test_real_producer_emits_a_capped_signal_carrying_its_reward_floor(
        monkeypatch, name, build_df, strategy_cls, expected_floor):
    """Proves the fixture actually triggers the strategy's real capped-target
    path (score >= threshold, take_profit=True) and that the REAL evaluate()
    call -- not a test-side assumption -- wires min_reward_r onto the Signal.
    """
    sig, _ = _real_signal(monkeypatch, name, build_df, strategy_cls)
    assert sig.triggered, f"fixture did not trigger a signal: {sig.notes}"
    assert sig.take_profit is True, f"fixture did not reach the capped-target path: {sig.notes}"
    assert sig.min_reward_r == expected_floor


@pytest.mark.parametrize("name,build_df,strategy_cls,expected_floor", REAL_PRODUCERS)
def test_real_producer_order_is_skipped_when_reward_drifts_below_floor(
        monkeypatch, name, build_df, strategy_cls, expected_floor):
    sig, df = _real_signal(monkeypatch, name, build_df, strategy_cls)
    assert sig.take_profit and sig.min_reward_r is not None    # fixture sanity
    drifted_quote = sig.entry_price * (1 + 0.014)   # inside the 1.5% chase guard
    broker, _ = _run_check_entries(monkeypatch, drifted_quote, name, sig, df)
    broker.place_market_buy.assert_not_called()


@pytest.mark.parametrize("name,build_df,strategy_cls,expected_floor", REAL_PRODUCERS)
def test_real_producer_order_still_submits_when_the_quote_has_not_drifted(
        monkeypatch, name, build_df, strategy_cls, expected_floor):
    """Control: proves the harness can reach order submission for this
    strategy's real signal, so the skip above is the guard firing."""
    sig, df = _real_signal(monkeypatch, name, build_df, strategy_cls)
    broker, portfolio = _run_check_entries(
        monkeypatch, sig.entry_price, name, sig, df)
    broker.place_market_buy.assert_called_once()
    portfolio.add_position.assert_called_once()


# --- Centralized wiring: proves ALL seven Signal(...) call sites at once ---
#
# Codex round 5 (2026-08-05): building a real evaluate()-triggering fixture
# for every one of double_bottom's 3 modes, trend_reversal's 2, and
# resistance_breakout's 2 (7 total capped-target call sites) is not
# tractable as 7 separate synthetic-pattern reconstructions. Instead,
# _cap_target_at_resistance/_overhead_cap (strategies/double_bottom.py,
# strategies/resistance_breakout.py) were changed to RETURN min_reward_r as
# part of their tuple, and every one of the 7 call sites now unpacks it from
# there instead of hardcoding the module constant separately -- dropping it
# from an unpacking assignment is an immediate ValueError, not a silently
# missing kwarg. These tests exercise the two centralized functions directly,
# across every branch (capped, not-capped/no-wall, no-room, and rb's off/
# skip/cap modes), which structurally covers all 7 call sites without 7
# separate full-pattern fixtures.

def _small_overhead_df(wick_high=None, n=40):
    """Minimal df for find_overhead_resistance: flat baseline, optionally two
    isolated high-wick touches forming a real 2-touch overhead zone. Baseline
    high stays BELOW close (not close+epsilon): a flat AT/above-close high on
    every bar is tie-tolerant-pivot noise that can itself register as a
    phantom "overhead level" a hair above close, producing a false take_
    profit=True in the "no wall" case."""
    idx = pd.date_range("2026-08-05 10:00", periods=n, freq="1min", tz=ET)
    close = np.full(n, 1.00)
    high = close - 0.001
    low = close - 0.002
    if wick_high is not None:
        high[10] = wick_high
        high[25] = wick_high
    return pd.DataFrame({"open": close, "high": high, "low": low,
                          "close": close, "volume": [1000] * n}, index=idx)


def test_cap_target_at_resistance_always_returns_the_real_constant():
    entry, stop = 1.00, 0.97
    blind_target = entry + 2 * (entry - stop)   # 1.06, room for a wall to matter

    df_no_wall = _small_overhead_df(wick_high=None)
    t, tp, room, mrr = double_bottom._cap_target_at_resistance(
        df_no_wall, entry, stop, blind_target, atr=0.01)
    assert tp is False and mrr == double_bottom.RES_CAP_MIN_REWARD_R

    df_capped = _small_overhead_df(wick_high=1.05)   # between entry and blind target
    t, tp, room, mrr = double_bottom._cap_target_at_resistance(
        df_capped, entry, stop, blind_target, atr=0.01)
    assert tp is True and mrr == double_bottom.RES_CAP_MIN_REWARD_R

    # jammed-under-wall case: wall barely above entry -> no room
    df_jammed = _small_overhead_df(wick_high=1.005)
    t, tp, room, mrr = double_bottom._cap_target_at_resistance(
        df_jammed, entry, stop, blind_target, atr=0.01)
    assert room is False and mrr == double_bottom.RES_CAP_MIN_REWARD_R


def test_overhead_cap_always_returns_the_real_constant(monkeypatch):
    entry, stop = 1.00, 0.97
    blind_target = entry + 2 * (entry - stop)
    traded_level = 0.995
    levels_no_wall = [(traded_level, None)]
    levels_with_wall = [(traded_level, None), (1.05, None)]
    levels_jammed = [(traded_level, None), (1.005, None)]

    monkeypatch.setattr(resistance_breakout, "OVERHEAD_CAP", "off")
    t, tp, room, mrr = resistance_breakout._overhead_cap(
        levels_with_wall, traded_level, entry, stop, blind_target)
    assert tp is False and mrr == resistance_breakout.RES_CAP_MIN_REWARD_R

    monkeypatch.setattr(resistance_breakout, "OVERHEAD_CAP", "cap")
    t, tp, room, mrr = resistance_breakout._overhead_cap(
        levels_no_wall, traded_level, entry, stop, blind_target)
    assert tp is False and mrr == resistance_breakout.RES_CAP_MIN_REWARD_R   # no overhead level

    t, tp, room, mrr = resistance_breakout._overhead_cap(
        levels_with_wall, traded_level, entry, stop, blind_target)
    assert tp is True and mrr == resistance_breakout.RES_CAP_MIN_REWARD_R

    t, tp, room, mrr = resistance_breakout._overhead_cap(
        levels_jammed, traded_level, entry, stop, blind_target)
    assert room is False and mrr == resistance_breakout.RES_CAP_MIN_REWARD_R

    monkeypatch.setattr(resistance_breakout, "OVERHEAD_CAP", "skip")
    t, tp, room, mrr = resistance_breakout._overhead_cap(
        levels_with_wall, traded_level, entry, stop, blind_target)
    assert tp is False and mrr == resistance_breakout.RES_CAP_MIN_REWARD_R   # room -> keeps 2R


# --- Genuinely structural coverage (round 6) --------------------------------
#
# Codex round 6 (2026-08-05) correctly rejected the round-5 "structural"
# claim: returning min_reward_r from the two capping functions stops a call
# site from referencing the WRONG constant, but nothing stopped a call site
# from correctly unpacking it and then simply not passing it into its own
# Signal(...) -- an omission, which is exactly the failure mode a "some
# modes untested" gap would hide. Fixed by removing the option to omit it:
# strategies/base.py:build_capped_signal() is now the ONLY function in the
# codebase that constructs a take_profit=True Signal, and all 7 real call
# sites (double_bottom.py x3, trend_reversal.py x2, resistance_breakout.py
# x2) were migrated to it. These two tests prove that claim directly, rather
# than asserting it in a comment: (1) the factory itself transfers the tuple
# correctly, and (2) NO strategy source file constructs a capped Signal
# outside the factory -- so there is no remaining call site left to omit
# anything from, regardless of which pattern-detection mode reaches it.

def test_build_capped_signal_transfers_the_cap_result_exactly():
    from strategies.base import build_capped_signal
    cap_result = (1.10, True, True, 0.42)   # target, take_profit, room_ok, min_reward_r
    sig = build_capped_signal(
        ticker="TEST", strategy="whatever", score=5, entry_price=1.00,
        notes="n", stop_price=0.97, cap_result=cap_result, ignore_vwap=True,
    )
    assert sig.target_price == 1.10
    assert sig.take_profit is True
    assert sig.min_reward_r == 0.42
    assert sig.stop_price == 0.97
    assert sig.ignore_vwap is True   # **extra forwarded correctly


def _src_of(rel_path):
    with open(os.path.join(REPO, rel_path), encoding="utf-8") as f:
        return f.read()


# AST-based, not string matching (Codex round 7, 2026-08-05): the round-6
# version only prohibited the literal substrings "take_profit=tp" and
# "min_reward_r=mrr" -- a call site rewritten as, say,
# `Signal(..., target_price=cap_result[0])` under a different local variable
# name would silently defeat both that check AND the string-only guarantee,
# reproducing the original omission defect while every existing test (which
# only exercises breakout mode per strategy) stayed green. This walks the
# real AST of each file and asserts, by call-site COUNT, that all seven
# known factory calls are still present, and separately that no `Signal(...)`
# call anywhere in these three files sets take_profit/min_reward_r directly
# -- catching a reverted or newly-added direct-construction call site
# regardless of variable naming.

_EXPECTED_FACTORY_CALLS = {
    "strategies/double_bottom.py": 3,       # early, retest, breakout
    "strategies/trend_reversal.py": 2,      # breakout, retest
    "strategies/resistance_breakout.py": 2,  # retest, breakout
}


def _call_target_name(node):
    """Best-effort function/attribute name a Call node is invoking."""
    import ast
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_every_expected_factory_call_site_is_present():
    import ast
    for path, expected in _EXPECTED_FACTORY_CALLS.items():
        tree = ast.parse(_src_of(path), filename=path)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and _call_target_name(n) == "build_capped_signal"]
        assert len(calls) == expected, (
            f"{path}: expected {expected} build_capped_signal(...) call sites, "
            f"found {len(calls)} -- a capped-target producer mode may have "
            f"been reverted to constructing Signal(...) directly"
        )


def test_no_signal_call_anywhere_sets_take_profit_or_min_reward_r_directly():
    """The ONLY function allowed to construct a take_profit=True Signal is
    build_capped_signal itself (box_range.py is the one legitimate
    exception: it sets take_profit=True directly, since it has no separate
    "cap result" tuple to route through this factory -- excluded below)."""
    import ast
    for path in ("strategies/double_bottom.py", "strategies/trend_reversal.py",
                 "strategies/resistance_breakout.py"):
        tree = ast.parse(_src_of(path), filename=path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_target_name(node) == "Signal"):
                continue
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            assert "take_profit" not in kw_names, (
                f"{path}:{node.lineno}: Signal(...) sets take_profit= directly "
                f"-- must go through build_capped_signal instead"
            )
            assert "min_reward_r" not in kw_names, (
                f"{path}:{node.lineno}: Signal(...) sets min_reward_r= directly "
                f"-- must go through build_capped_signal instead"
            )
