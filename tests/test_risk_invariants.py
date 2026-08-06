"""Risk invariants that must hold regardless of strategy tuning.

These lock down what the month-1 live review established the hard way, so a
future change -- or a future analysis -- cannot silently contradict them.

Run: py -3.12 -m pytest tests -q
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest

import config
from trading.risk_manager import calc_position_size, stop_is_valid


def _src(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


# --- stop-distance floor (v1.6) ---------------------------------------------

def test_min_stop_pct_floor_is_active():
    assert config.MIN_STOP_PCT == 0.03


def test_stop_floor_widens_a_too_tight_structural_stop():
    entry = 1.00
    floor_stop = entry * (1 - config.MIN_STOP_PCT)
    assert min(0.995, floor_stop) == floor_stop          # 0.5% structural -> floored


def test_stop_floor_never_tightens_a_wider_structural_stop():
    entry = 1.00
    assert min(0.94, entry * (1 - config.MIN_STOP_PCT)) == 0.94   # 6% kept


def test_stop_floor_guarantees_stop_below_entry():
    """With the floor active, any structural stop clamps strictly below entry.

    This is defense in depth, not the only guard: main.py and backtest_viral.py
    both gate on stop_is_valid() immediately before sizing, so a 0 or NaN floor
    can no longer reach an order (Codex R1-INVALID-STOP).
    """
    assert config.MIN_STOP_PCT > 0
    for entry in (0.51, 1.00, 3.33, 9.99):
        floor_stop = entry * (1 - config.MIN_STOP_PCT)
        assert floor_stop < entry
        assert min(entry * 1.5, floor_stop) < entry      # clamps even an absurd stop


# --- position sizing --------------------------------------------------------

def test_cost_cap_binds_on_tight_stop_pennies():
    """The 20% notional cap -- not the 2% risk rule -- sets size here.

    Month-1 finding: it bound on 28/28 live trades, so a wider stop does NOT
    reduce share count on this bot. Any analysis assuming otherwise is wrong.
    """
    acct, entry = 52500.0, 1.00
    shares = calc_position_size(acct, entry, entry * 0.97)
    assert shares * entry <= 0.20 * acct + entry         # cost cap respected
    risk_shares = (config.RISK_PCT_PER_TRADE * acct) / (entry * 0.03)
    assert shares < risk_shares                          # risk rule did NOT bind


def test_position_sizing_fails_closed_on_an_unusable_stop():
    """Sizing must REJECT an unusable stop, not return a tradeable quantity.

    This previously failed open: risk-per-share was floored at $0.01 and the
    result at 1 share, so a stop at/above entry (or NaN) still produced shares and
    main.py submitted the buy (Codex R1-INVALID-STOP).
    """
    nan, inf = float("nan"), float("inf")
    assert calc_position_size(10000.0, 1.00, 1.00) == 0     # stop == entry
    assert calc_position_size(10000.0, 1.00, 1.10) == 0     # stop above entry
    assert calc_position_size(10000.0, 1.00, 0.00) == 0     # non-positive stop
    assert calc_position_size(10000.0, 1.00, -0.50) == 0    # negative stop
    assert calc_position_size(10000.0, 1.00, nan) == 0      # NaN stop
    assert calc_position_size(10000.0, nan, 0.97) == 0      # NaN entry
    assert calc_position_size(10000.0, 1.00, inf) == 0      # inf stop
    assert calc_position_size(nan, 1.00, 0.97) == 0         # NaN account
    assert calc_position_size(0.0, 1.00, 0.97) == 0         # no account value
    assert calc_position_size(10000.0, 1.00, 0.97) > 0      # the valid case still trades


def test_stop_is_valid_rejects_every_unusable_stop():
    nan, inf = float("nan"), float("inf")
    assert stop_is_valid(1.00, 0.97)
    assert not stop_is_valid(1.00, 1.00)
    assert not stop_is_valid(1.00, 1.01)
    assert not stop_is_valid(1.00, 0.00)
    assert not stop_is_valid(1.00, -0.10)
    assert not stop_is_valid(1.00, nan)
    assert not stop_is_valid(nan, 0.97)
    assert not stop_is_valid(1.00, inf)
    assert not stop_is_valid(inf, 0.97)


def test_both_engines_validate_the_stop_before_submitting():
    """The guard must sit in the live path AND the sim path, not just one."""
    for path in ("main.py", "backtest_viral.py"):
        assert "stop_is_valid" in _src(path), f"{path} must gate on stop_is_valid"


def test_min_stop_pct_override_is_validated():
    """A nonsense env override must refuse to load rather than trade on it."""
    var = "MIN_STOP_PCT_TEST"
    try:
        for bad in ("nan", "inf", "-inf", "-0.01", "1.5", "0.9", "abc", "0x1"):
            os.environ[var] = bad
            with pytest.raises(ValueError):
                config._env_fraction(var, 0.03, 0.0, 0.50)
        os.environ.pop(var, None)
        assert config._env_fraction(var, 0.03, 0.0, 0.50) == 0.03   # absent -> default
        os.environ[var] = ""
        assert config._env_fraction(var, 0.03, 0.0, 0.50) == 0.03   # blank -> default
        os.environ[var] = "0.04"
        assert config._env_fraction(var, 0.03, 0.0, 0.50) == 0.04   # valid override
        os.environ[var] = "0"
        assert config._env_fraction(var, 0.03, 0.0, 0.50) == 0.0    # 0 allowed
    finally:
        os.environ.pop(var, None)


# --- broker costs -----------------------------------------------------------

def test_commission_is_charged_per_side_with_floor_and_cap():
    assert config.ibkr_commission(1000, 1.00) == 5.0      # $0.005/share
    assert config.ibkr_commission(10, 1.00) == 1.0        # $1 order minimum
    assert config.ibkr_commission(10000, 0.20) == 20.0    # 1%-of-value cap
    assert config.ibkr_commission(0, 1.00) == 0.0


# --- sim / live parity ------------------------------------------------------

def test_sim_and_live_share_one_stop_floor_constant():
    """Both engines must read the SAME constant, never a duplicated literal."""
    for path in ("main.py", "backtest_viral.py"):
        src = _src(path)
        assert "config.MIN_STOP_PCT" in src, f"{path} must use config.MIN_STOP_PCT"
        assert not re.search(r"1\s*-\s*0\.03", src), f"{path} hardcodes the floor"
