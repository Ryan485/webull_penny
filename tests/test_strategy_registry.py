"""The strategy on/off switch must fail loudly, never silently.

An experiment that turns strategies off is only safe if (a) turning one off
cannot delete it, (b) a typo cannot leave the bot scanning with nothing armed,
and (c) live and backtest build from the same spec.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest

import config
from strategies.registry import REGISTRY, build_strategies, parse_spec


def _src(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


def test_default_set_is_the_frozen_three():
    """The frozen v1.6 set stays the default -- an experiment must be opt-in."""
    assert parse_spec(None) == [
        "double_bottom", "trend_reversal", "resistance_breakout"
    ]


def test_turning_a_strategy_off_does_not_delete_it():
    """Off means 'not built', not 'gone'. Every live-eligible strategy stays
    registered when some other strategy is the one actually enabled."""
    for name in ("double_bottom", "trend_reversal", "resistance_breakout",
                 "box_range"):
        assert name in REGISTRY
    built = [s.name for s in build_strategies("box_range")]
    assert built == ["box_range"]                 # only box_range runs...
    assert "double_bottom" in REGISTRY            # ...but the others still exist


def test_retired_gap_bounce_is_not_live_activatable():
    """gap_bounce is retired from the live loop entirely, not just off by
    default -- unlike the strategies above, it must not be in REGISTRY at all,
    so an env typo or stale setting can never arm it for real order submission
    (Codex NEW-RETIRED-GAP-BOUNCE-ACTIVATABLE, 2026-08-04)."""
    assert "gap_bounce" not in REGISTRY
    with pytest.raises(ValueError):
        build_strategies("gap_bounce")


def test_a_bad_spec_raises_instead_of_arming_nothing():
    """A typo must not look like a quiet trading day."""
    for bad in ("", "   ", ",,,", "box_rnage", "double_bottom,nope"):
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_spec_is_deduped_and_order_preserved():
    assert parse_spec("box_range, double_bottom ,box_range") == [
        "box_range", "double_bottom"
    ]


def test_live_and_backtest_build_from_the_same_switch():
    """Neither engine may hardcode its own strategy list."""
    for path in ("main.py", "backtest_viral.py"):
        src = _src(path)
        assert "build_strategies()" in src, f"{path} must use the registry"
        assert "[DoubleBottom()" not in src, f"{path} hardcodes a strategy list"


def test_enabled_strategies_is_configurable():
    assert isinstance(config.ENABLED_STRATEGIES, str)
    assert config.ENABLED_STRATEGIES.strip()
