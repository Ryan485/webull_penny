"""Regression tests for the box_range (박스권매매) experiment.

Locks down three findings from Codex review round 1 (2026-08-04):
  NEW-BOX-CONFIG-FAILS-OPEN   - box_range's env overrides must fail closed,
                                 same as MIN_STOP_PCT.
  NEW-BOX-REWARD-QUOTE-DRIFT  - box_range's reward floor must be revalidated
                                 against the executable broker quote, not just
                                 the candle close the signal was built from.
  NEW-STRATEGY-VERSION-MIX    - a non-default ENABLED_STRATEGIES must not log
                                 trades under the same version tag as the
                                 frozen default set.

Run: py -3.12 -m pytest tests -q
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest

import config
import strategies.box_range as box_range


def _isolated(code: str, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run `code` in a real, separate py -3.12 process from REPO, with a clean
    environment plus env_overrides. Used where a monkeypatched attribute or a
    validator called directly (matching bounds by hand) is not proof enough
    that the ACTUAL module, imported fresh, behaves correctly -- Codex round 2
    rejected exactly that weaker evidence for NEW-BOX-CONFIG-FAILS-OPEN."""
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env,
        capture_output=True, text=True, timeout=30,
    )


def _src(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


# --- NEW-BOX-CONFIG-FAILS-OPEN ----------------------------------------------
#
# Codex round 2 (2026-08-04) explicitly rejected the round-1 versions of these
# tests: they called config._env_int/_env_fraction directly with bounds
# copied BY HAND into the test, which proves the VALIDATOR works but not that
# box_range.py actually wires it up with those same bounds. These now
# construct the REAL module (and the real BoxRange()) in an isolated
# subprocess, so a mismatched bound in the actual source would be caught.

def test_min_touches_floor_enforces_the_owner_requirement():
    assert box_range.MIN_TOUCHES >= 2
    for bad in ("0", "1", "-1", "nan", "abc", "11"):
        r = _isolated(
            "import strategies.box_range as b; b.BoxRange()",
            {"ENABLED_STRATEGIES": "box_range", "BOX_MIN_TOUCHES": bad},
        )
        assert r.returncode != 0, f"BOX_MIN_TOUCHES={bad} should have failed closed"
        assert "ValueError" in r.stderr


def test_box_float_overrides_fail_closed_on_real_isolated_import():
    """Every box_range float knob must reject NaN/inf/out-of-range in the REAL
    module when box_range is actually constructed, not trade on it."""
    cases = [
        ("BOX_MIN_HEIGHT_PCT", ("nan", "inf", "-1", "0.6")),
        ("BOX_MAX_HEIGHT_PCT", ("nan", "inf", "-1", "1.1")),
        ("BOX_ENTRY_ZONE", ("nan", "inf", "0", "1.1")),
        ("BOX_STOP_ATR_MULT", ("nan", "inf", "0", "-1")),
        # 0 must now be REJECTED (Codex round 2): a strategy-defining reward
        # floor of zero is not "aggressive", it is "disabled" -- unlike
        # MIN_STOP_PCT, box_range has no independent safety net behind it.
        ("BOX_MIN_REWARD_R", ("nan", "inf", "-1", "0")),
    ]
    for name, bad_values in cases:
        for bad in bad_values:
            r = _isolated(
                "import strategies.box_range as b; b.BoxRange()",
                {"ENABLED_STRATEGIES": "box_range", name: bad},
            )
            assert r.returncode != 0, f"{name}={bad} should have failed closed"
            assert "ValueError" in r.stderr


def test_box_valid_overrides_construct_cleanly():
    """The floor above must not be so tight it rejects legitimate tuning."""
    r = _isolated(
        "import strategies.box_range as b; b.BoxRange(); print('OK')",
        {"ENABLED_STRATEGIES": "box_range", "BOX_MIN_REWARD_R": "0.5",
         "BOX_ENTRY_ZONE": "0.5", "BOX_MIN_TOUCHES": "3"},
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_min_height_must_be_below_max_height():
    """An inverted geometry window would reject every box; must refuse to load."""
    assert box_range.MIN_HEIGHT_PCT < box_range.MAX_HEIGHT_PCT
    r = _isolated(
        "import strategies.box_range as b; b.BoxRange()",
        {"ENABLED_STRATEGIES": "box_range",
         "BOX_MIN_HEIGHT_PCT": "0.5", "BOX_MAX_HEIGHT_PCT": "0.3"},
    )
    assert r.returncode != 0
    assert "must be <" in r.stderr


# --- NEW-DISABLED-BOX-CONFIG-STARTUP ----------------------------------------

def test_default_strategies_start_despite_a_stale_bad_box_override():
    """A leftover invalid BOX_* var from an old experiment must not brick a
    restart of the DEFAULT (box_range-disabled) strategy set -- including a
    restart needed only to resume managing already-open positions."""
    r = _isolated(
        "import main; print([s.name for s in main.STRATEGIES])",
        {"BOX_MIN_TOUCHES": "1"},   # invalid: below the floor of 2
    )
    assert r.returncode == 0, r.stderr
    assert "double_bottom" in r.stdout


def test_box_range_itself_still_fails_closed_when_actually_enabled():
    """The startup fix above must not accidentally make box_range's own
    validation inert when it IS the active strategy."""
    r = _isolated(
        "import main",
        {"ENABLED_STRATEGIES": "box_range", "BOX_MIN_TOUCHES": "1"},
    )
    assert r.returncode != 0
    assert "ValueError" in r.stderr


# --- NEW-BOX-REWARD-QUOTE-DRIFT ---------------------------------------------

def test_reward_at_close_can_pass_while_reward_at_live_quote_fails():
    """Reproduces the exact Codex-reported drift: 1.06R at the signal candle's
    close, ~0.37R at an allowed post-chase-guard quote for the SAME trade.
    """
    close, stop, target = 1.005, 0.97485, 1.03688
    risk_at_close = close - stop
    reward_at_close = target - close
    assert reward_at_close / risk_at_close >= box_range.MIN_REWARD_R  # passes at close

    live_quote = 1.020  # within MAX_ENTRY_CHASE_PCT of close (1.5%)
    assert live_quote <= close * (1 + config.MAX_ENTRY_CHASE_PCT)
    risk_at_quote = live_quote - stop
    reward_at_quote = target - live_quote
    assert reward_at_quote / risk_at_quote < box_range.MIN_REWARD_R  # fails at the fill
    assert reward_at_quote / risk_at_quote == pytest.approx(0.374, abs=0.01)


def test_reward_drift_guard_is_wired_into_main():
    """main.py must revalidate a capped signal's reward floor against
    live_price, not just trust the value computed at the strategy's candle
    close. Generalized (Codex NEW-CAPPED-TARGET-QUOTE-DRIFT, round 2) from a
    box_range-only check to any take_profit=True signal carrying
    min_reward_r -- see tests/test_reward_quote_drift.py for the behavioral
    (mocked check_entries) proof that this is actually wired in, not just
    present as source text."""
    src = _src("main.py")
    assert "best.take_profit and best.min_reward_r is not None" in src
    # must compare against the LIVE price/stop, not the stale close-time values
    assert "target - live_price" in src


# --- NEW-STRATEGY-VERSION-MIX -----------------------------------------------

def test_default_strategy_set_keeps_the_frozen_version():
    assert config.OUTCOME_VERSION == config.STRATEGY_VERSION


def test_non_default_strategy_set_gets_a_distinct_version():
    custom = config._normalize_strategy_spec("box_range")
    default = config._normalize_strategy_spec(config._DEFAULT_STRATEGIES)
    assert custom != default
    tag = (config.STRATEGY_VERSION if custom == default
           else f"{config.STRATEGY_VERSION}+strategies:{custom}")
    assert tag != config.STRATEGY_VERSION
    assert "box_range" in tag


def test_normalize_strategy_spec_dedupes_and_trims_but_keeps_order():
    """Order matters functionally (first-registered wins score ties in
    check_entries), so two specs that differ only in order must NOT collapse to
    the same identity -- only whitespace/duplicate noise should be ignored."""
    assert (config._normalize_strategy_spec("box_range, double_bottom ,box_range")
            == "box_range,double_bottom")
    assert (config._normalize_strategy_spec("box_range,double_bottom")
            != config._normalize_strategy_spec("double_bottom,box_range"))


def test_outcome_version_is_what_gets_logged():
    """Every logged trade must use the version that reflects the ACTIVE set,
    not the frozen tag, so switching strategies can never mix forward-test
    samples under one label."""
    assert "config.OUTCOME_VERSION" in _src("trading/portfolio.py")
    assert "config.STRATEGY_VERSION" not in _src("trading/portfolio.py")


# --- NEW-BOX-PARAM-VERSION-MIX (round 3) ------------------------------------
#
# Codex round 3 rejected the round-2 fix: it compared "was the env var SET",
# so an unset BOX_ENTRY_ZONE and an explicit BOX_ENTRY_ZONE=0.33 (its own
# default) produced DIFFERENT tags -- violating "unchanged defaults stay
# stable". Fixed with config._BOX_DEFAULTS (effective-value comparison). These
# exercise the REAL config.OUTCOME_VERSION in isolated subprocesses, not a
# hand-rebuilt comparison in the test.

def test_box_defaults_table_matches_the_real_module():
    """config._BOX_DEFAULTS is a duplicated literal (box_range.py cannot be
    imported back into config.py); if it drifts from the real module's
    defaults, the version fingerprint silently starts lying. Guard the sync."""
    assert config._BOX_DEFAULTS["BOX_MIN_TOUCHES"][0] == box_range.MIN_TOUCHES
    assert config._BOX_DEFAULTS["BOX_MIN_HEIGHT_PCT"][0] == box_range.MIN_HEIGHT_PCT
    assert config._BOX_DEFAULTS["BOX_MAX_HEIGHT_PCT"][0] == box_range.MAX_HEIGHT_PCT
    assert config._BOX_DEFAULTS["BOX_MIN_BARS"][0] == box_range.MIN_BOX_BARS
    assert config._BOX_DEFAULTS["BOX_ENTRY_ZONE"][0] == box_range.ENTRY_ZONE
    assert config._BOX_DEFAULTS["BOX_STOP_ATR_MULT"][0] == box_range.STOP_ATR_MULT
    assert config._BOX_DEFAULTS["BOX_MIN_REWARD_R"][0] == box_range.MIN_REWARD_R


def test_explicit_default_value_produces_the_same_version_as_unset():
    r_unset = _isolated("import config; print(config.OUTCOME_VERSION)",
                         {"ENABLED_STRATEGIES": "box_range"})
    r_explicit_default = _isolated(
        "import config; print(config.OUTCOME_VERSION)",
        {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": "0.33"},
    )
    assert r_unset.returncode == 0 and r_explicit_default.returncode == 0
    assert r_unset.stdout.strip() == r_explicit_default.stdout.strip()


def test_different_effective_value_produces_a_different_version():
    r_default = _isolated("import config; print(config.OUTCOME_VERSION)",
                           {"ENABLED_STRATEGIES": "box_range"})
    r_changed = _isolated("import config; print(config.OUTCOME_VERSION)",
                           {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": "1.0"})
    assert r_default.returncode == 0 and r_changed.returncode == 0
    assert r_default.stdout.strip() != r_changed.stdout.strip()
    assert "BOX_ENTRY_ZONE=1.0" in r_changed.stdout


def test_boolean_box_flag_also_compares_effective_value():
    """BOX_IGNORE_VWAP=1 is the default (true) spelled out explicitly -- must
    not diverge from leaving it unset, same principle as the numeric knobs."""
    r_unset = _isolated("import config; print(config.OUTCOME_VERSION)",
                         {"ENABLED_STRATEGIES": "box_range"})
    r_explicit_default = _isolated("import config; print(config.OUTCOME_VERSION)",
                                    {"ENABLED_STRATEGIES": "box_range", "BOX_IGNORE_VWAP": "1"})
    r_changed = _isolated("import config; print(config.OUTCOME_VERSION)",
                           {"ENABLED_STRATEGIES": "box_range", "BOX_IGNORE_VWAP": "0"})
    assert r_unset.stdout.strip() == r_explicit_default.stdout.strip()
    assert r_unset.stdout.strip() != r_changed.stdout.strip()


def test_whitespace_padded_and_present_blank_ignore_vwap_are_not_the_default():
    """Codex round 6 (2026-08-05): the round-5 fix was correct in production
    (box_range.py's real, unstripped `os.environ.get(name, "1") == "1"`
    already treats " 1 " and "" as NOT matching "1", i.e. IGNORE_VWAP=False),
    but nothing pinned it down -- reintroducing a `.strip()` on this one var
    would make this test suite pass again while silently breaking it. Checks
    BOTH the real module attribute (production behavior) AND OUTCOME_VERSION
    (forward-test bookkeeping) so a regression in either direction is caught.
    """
    for bad_raw in (" 1 ", ""):
        r = _isolated(
            "import config, strategies.box_range as b; "
            "print(b.IGNORE_VWAP); print(config.OUTCOME_VERSION)",
            {"ENABLED_STRATEGIES": "box_range", "BOX_IGNORE_VWAP": bad_raw},
        )
        assert r.returncode == 0, r.stderr
        lines = r.stdout.strip().splitlines()
        assert lines[0] == "False", (
            f"BOX_IGNORE_VWAP={bad_raw!r} must NOT match the default (real "
            f"IGNORE_VWAP should be False, matching box_range.py's own "
            f"unstripped comparison), got: {r.stdout!r}"
        )
        assert "box:BOX_IGNORE_VWAP=0" in lines[1], (
            f"BOX_IGNORE_VWAP={bad_raw!r} produces real IGNORE_VWAP=False, "
            f"which differs from the default (True) -- OUTCOME_VERSION must "
            f"reflect that, got: {lines[1]!r}"
        )

    # And the genuinely-default case must still collapse correctly.
    r_unset = _isolated(
        "import config, strategies.box_range as b; "
        "print(b.IGNORE_VWAP); print(config.OUTCOME_VERSION)",
        {"ENABLED_STRATEGIES": "box_range"},
    )
    lines = r_unset.stdout.strip().splitlines()
    assert lines[0] == "True"
    assert "BOX_IGNORE_VWAP" not in lines[1]


# --- NEW-DEFAULT-STRATEGY-VERSION-NOT-BUMPED --------------------------------

def test_strategy_version_was_bumped_for_the_reward_guard_change():
    """The generalized reward-drift guard changes what the DEFAULT (box_range-
    disabled) strategy set actually submits -- a signal that cleared its
    reward floor at the candle close can now be skipped after live-quote or
    stop-floor revalidation, where it previously would have been submitted.
    That is a real signal-logic change to v1.6, not a config/tooling change,
    so it must not keep stamping trades with the old identifier (Codex
    NEW-DEFAULT-STRATEGY-VERSION-NOT-BUMPED, 2026-08-04)."""
    assert config.STRATEGY_VERSION != "us-penny-v1.6-stopfloor3-2026-07-24"
    assert config.OUTCOME_VERSION.startswith(config.STRATEGY_VERSION)


# --- NEW-BOX-PARAM-VERSION-MIX (round 5: canonicalization) ------------------
#
# Codex round 5 rejected the round-4 fix: it compared EFFECTIVE values for
# equality but then emitted the RAW TEXT into the fingerprint, so
# BOX_ENTRY_ZONE=.5 and BOX_ENTRY_ZONE=0.50 -- the identical effective float
# -- produced different fingerprints and therefore different OUTCOME_VERSION
# tags. Fixed by emitting repr(effective) instead of raw.

def test_equivalent_numeric_representations_produce_the_same_version():
    r_dot5 = _isolated("import config; print(config.OUTCOME_VERSION)",
                        {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": ".5"})
    r_0_50 = _isolated("import config; print(config.OUTCOME_VERSION)",
                        {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": "0.50"})
    r_0_5 = _isolated("import config; print(config.OUTCOME_VERSION)",
                       {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": "0.5"})
    assert r_dot5.returncode == r_0_50.returncode == r_0_5.returncode == 0
    assert r_dot5.stdout.strip() == r_0_50.stdout.strip() == r_0_5.stdout.strip()


def test_genuinely_different_values_still_diverge():
    """Guards against a canonicalization bug that collapses everything --
    a real difference must still produce a real difference."""
    r_default = _isolated("import config; print(config.OUTCOME_VERSION)",
                           {"ENABLED_STRATEGIES": "box_range"})
    r_changed = _isolated("import config; print(config.OUTCOME_VERSION)",
                           {"ENABLED_STRATEGIES": "box_range", "BOX_ENTRY_ZONE": "1.0"})
    assert r_default.stdout.strip() != r_changed.stdout.strip()
