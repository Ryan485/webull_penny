"""Single source of truth for WHICH strategies are active.

Every entry point (main.py, backtest_viral.py, debug_entries.py) builds its
strategy list from here, so the live bot and the backtest can never silently
disagree about what is running.

Turning a strategy off does NOT delete it: the class stays registered and
importable, it simply is not built. Flip the set with config.ENABLED_STRATEGIES
(env var ENABLED_STRATEGIES), e.g.

    ENABLED_STRATEGIES=box_range py -3.12 backtest_viral.py

Unknown or empty names raise instead of silently trading nothing -- a typo that
disables every strategy would otherwise look exactly like a quiet day.
"""
from typing import Dict, List, Optional, Type

import config
from strategies.base import BaseStrategy
from strategies.box_range import BoxRange
from strategies.double_bottom import DoubleBottom
from strategies.resistance_breakout import ResistanceBreakout
from strategies.trend_reversal import TrendReversal

# Every strategy eligible to run LIVE via config.ENABLED_STRATEGIES. Membership
# here is not activation -- box_range is an experiment, off by default. Deliberately
# does NOT include gap_bounce: it is retired from the live loop entirely (owner
# decision, see CLAUDE.md) and lives only in backtesting/engine.py's own separate,
# hardcoded strategy list, which does not go through this registry. Registering it
# here would let ENABLED_STRATEGIES=gap_bounce arm retired signal logic for real
# order submission (Codex NEW-RETIRED-GAP-BOUNCE-ACTIVATABLE, 2026-08-04).
REGISTRY: Dict[str, Type[BaseStrategy]] = {
    DoubleBottom.name: DoubleBottom,
    TrendReversal.name: TrendReversal,
    ResistanceBreakout.name: ResistanceBreakout,
    BoxRange.name: BoxRange,
}


def parse_spec(spec: Optional[str] = None) -> List[str]:
    """Normalize a comma-separated spec into validated strategy names."""
    raw = config.ENABLED_STRATEGIES if spec is None else spec
    names = [n.strip() for n in str(raw).split(",") if n.strip()]
    if not names:
        raise ValueError(
            "ENABLED_STRATEGIES is empty - the bot would scan and never trade. "
            f"Valid names: {', '.join(sorted(REGISTRY))}"
        )
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(
            f"ENABLED_STRATEGIES has unknown name(s): {', '.join(unknown)}. "
            f"Valid names: {', '.join(sorted(REGISTRY))}"
        )
    seen, ordered = set(), []
    for n in names:                      # de-dupe, preserve order
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def build_strategies(spec: Optional[str] = None) -> List[BaseStrategy]:
    return [REGISTRY[n]() for n in parse_spec(spec)]
