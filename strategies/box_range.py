"""
Strategy 5: Box Range (박스권매매) — buy support, sell resistance.

EXPERIMENT (2026-08-04, owner directive). Off by default: it only runs when
config.ENABLED_STRATEGIES names it. Nothing else is deleted or changed.

This is the RANGE half of box trading. resistance_breakout already trades the
BREAK of the box top; this trades the box while it is still holding:

  - Resistance = 2+ swing-high touches clustered within 0.5%, >=10 bars apart
    (the owner's explicit "2+ tests" requirement; same detector convention as
    resistance_breakout, so the two strategies agree on what a level is)
  - Support    = the mirror image on swing LOWS, same 2+ touch requirement
  - The pair must behave like a real box: since both levels existed, every
    completed close has stayed INSIDE it (a close above the top is a breakout
    -> resistance_breakout's trade, not ours; a close below the bottom means
    the box failed and there is no support left to lean on)
  - Entry only in the BOTTOM third of the box, on a green bar bouncing off a
    low that actually tagged support. Buying mid-box is how you end up long
    into the middle of a range with no edge either way.
  - Stop  : support - 0.5 ATR, then widened by config.MIN_STOP_PCT (v1.6)
  - Target: just under the box top, sold as a REAL take-profit (take_profit=
    True). That is the point of range trading -- you exit at the top of the
    range, you do not trail through it and give it back.
  - The trade is skipped unless the room to the top is worth the risk
    (BOX_MIN_REWARD_R), measured AFTER the stop floor is applied, because the
    3% floor is usually what sets the real risk on these names.

Both levels are tie-tolerant (PIVOT_TIE_PCT): penny ranges print the same
high/low repeatedly, and that repetition IS the level -- strict inequalities
would reject exactly the boxes being hunted.
"""
import os
from typing import List, Optional, Tuple

import pandas as pd

import config
from strategies.base import BaseStrategy, Signal


WINDOW = 120                     # 2-hour lookback, same as the other structural
                                 # strategies
MIN_BARS = 45                    # tickers with no premarket bars only have ~80
                                 # bars by 11:00 ET; demanding a full WINDOW
                                 # silently disables the strategy all morning
                                 # (learned the hard way on LUCY 2026-07-06)
PIVOT = 5
PIVOT_TIE_PCT = 0.001            # equal highs/lows ARE the level
CLUSTER_PCT = 0.005              # touches within 0.5% form one level
MIN_TOUCH_GAP = 10               # ...and must be >=10 bars apart

BREAK_TOL = 0.005                # closes may poke 0.5% past a level without
                                 # counting as a break (wick noise)
TAG_LOOKBACK = 10                # the bounce low must be within this many bars
TAG_TOL_ATR = 1.0                # ...and must have actually reached support
CONFIRM_ATR = 0.5                # close >= bounce low + 0.5 ATR (bounce underway)
TARGET_BUFFER = 0.997            # sell a hair below the top; don't need the tag

# Config is loaded and validated in _load_config() below, NOT at plain module
# scope (Codex NEW-DISABLED-BOX-CONFIG-STARTUP, 2026-08-04): strategies/
# registry.py imports every strategy module eagerly so REGISTRY can list them
# all, including when box_range is NOT in config.ENABLED_STRATEGIES. A bare
# module-level `raise` on a bad BOX_* env var -- even one left over from an
# old experiment -- would have crashed startup of the DEFAULT strategy set
# too, including a restart needed only to manage already-open positions. The
# validation must fire when this strategy is actually constructed, not on
# unconditional import.
_CONFIG_ERROR = None
try:
    # Owner requirement (2026-08-04): a level counts only if it was TESTED 2+
    # times. Applies to BOTH sides -- an untested support is not a box floor.
    # Validated (Codex NEW-BOX-CONFIG-FAILS-OPEN): an unvalidated override
    # could set this to 0 or 1 and silently trade single-touch "levels",
    # defeating the entire point of the experiment. Floor of 2 enforces the
    # owner's requirement at the config layer, not just in the detector logic.
    MIN_TOUCHES = config._env_int("BOX_MIN_TOUCHES", 2, 2, 10)

    # Box geometry. The floor is a fee/noise constraint, not a curve fit: with
    # MIN_STOP_PCT at 3% and ~0.4-0.7% round-trip commission on $1-3 names, a
    # box thinner than ~4% cannot pay for its own risk. The ceiling mirrors
    # double_bottom's 20% depth cap -- past that it is a spike-and-collapse
    # being mislabelled a range, not a box.
    MIN_HEIGHT_PCT = config._env_fraction("BOX_MIN_HEIGHT_PCT", 0.04, 0.001, 0.50)
    MAX_HEIGHT_PCT = config._env_fraction("BOX_MAX_HEIGHT_PCT", 0.20, 0.01, 1.00)
    if MIN_HEIGHT_PCT >= MAX_HEIGHT_PCT:
        raise ValueError(
            f"BOX_MIN_HEIGHT_PCT ({MIN_HEIGHT_PCT}) must be < "
            f"BOX_MAX_HEIGHT_PCT ({MAX_HEIGHT_PCT}) or every box would be "
            f"rejected as either too thin or too tall with nothing in between"
        )

    MIN_BOX_BARS = config._env_int("BOX_MIN_BARS", 30, 1, WINDOW)
                                     # the box must have CONTAINED price this
                                     # long before it counts as established
    ENTRY_ZONE = config._env_fraction("BOX_ENTRY_ZONE", 0.33, 0.01, 1.00)
                                     # buy only in the bottom third of the box
    STOP_ATR_MULT = config._env_fraction("BOX_STOP_ATR_MULT", 0.5, 0.01, 5.00)
    # Codex NEW-BOX-CONFIG-FAILS-OPEN: lo was 0.0, which let BOX_MIN_REWARD_R=0
    # disable the strategy's own defining "is this trade worth the risk" gate
    # entirely -- unlike MIN_STOP_PCT, there is no independent safety net
    # backing this one up, so zero must not be a legal floor.
    MIN_REWARD_R = config._env_fraction("BOX_MIN_REWARD_R", 1.0, 0.1, 10.0)
except ValueError as e:
    _CONFIG_ERROR = e
    # Fall back to the known-safe defaults so the MODULE still imports (main.py
    # imports MIN_REWARD_R at startup unconditionally, whether or not box_range
    # ends up enabled) -- but BoxRange.__init__ raises _CONFIG_ERROR immediately
    # if this strategy is actually constructed, so an invalid override still
    # fails closed exactly when box_range is the strategy actually in use.
    MIN_TOUCHES, MIN_HEIGHT_PCT, MAX_HEIGHT_PCT = 2, 0.04, 0.20
    MIN_BOX_BARS, ENTRY_ZONE, STOP_ATR_MULT, MIN_REWARD_R = 30, 0.33, 0.5, 1.0

# A box bottom sits below session VWAP by nature (VWAP is dragged up by the
# spike that created the range), so enforcing the VWAP gate would block nearly
# every entry -- the same argument that won for double_bottom's early W entry.
# NOTE the counter-evidence: waiving VWAP for resistance_breakout's box-top
# RETEST was tested 2026-07-14 (LHAI) and REJECTED (PF 2.90 -> 2.22). That was
# a retest above a broken level, not a bounce off an intact floor, but it is a
# real warning -- this flag is swept, not assumed.
IGNORE_VWAP = os.environ.get("BOX_IGNORE_VWAP", "1") == "1"


def _pivots(values, kind: str) -> List[int]:
    """Tie-tolerant swing highs (kind='high') or swing lows (kind='low')."""
    out = []
    for i in range(PIVOT, len(values) - PIVOT):
        p = values[i]
        if kind == "high":
            cap = p * (1 + PIVOT_TIE_PCT)
            ok = all(values[i - k] <= cap and values[i + k] <= cap
                     for k in range(1, PIVOT + 1))
        else:
            floor = p * (1 - PIVOT_TIE_PCT)
            ok = all(values[i - k] >= floor and values[i + k] >= floor
                     for k in range(1, PIVOT + 1))
        if ok:
            out.append(i)
    return out


def _levels(values, kind: str) -> List[Tuple[float, List[int]]]:
    """Return [(level, touch_indices)] for every MIN_TOUCHES+ cluster.

    The level price is the CONSERVATIVE edge of the cluster: the max touch
    high for resistance, the min touch low for support. That is the price the
    market actually proved, and it keeps the stop below every tested low
    instead of inside the cluster.
    """
    pivots = _pivots(values, kind)
    if len(pivots) < MIN_TOUCHES:
        return []

    out: List[Tuple[float, List[int]]] = []
    for a_idx, a in enumerate(pivots):
        anchor = values[a]
        touches = [a]
        for b in pivots[a_idx + 1:]:
            if abs(values[b] - anchor) / anchor <= CLUSTER_PCT \
                    and b - touches[-1] >= MIN_TOUCH_GAP:
                touches.append(b)
        if len(touches) < MIN_TOUCHES:
            continue
        picked = [values[t] for t in touches]
        level = float(max(picked) if kind == "high" else min(picked))
        if not any(abs(level - lv) / lv <= CLUSTER_PCT for lv, _ in out):
            out.append((level, touches))
    return out


class BoxRange(BaseStrategy):
    name = "box_range"

    def __init__(self):
        # Only raised when box_range is actually constructed (i.e. actually
        # enabled) -- see the _CONFIG_ERROR comment above for why this is not
        # a module-level raise.
        if _CONFIG_ERROR is not None:
            raise _CONFIG_ERROR

    def evaluate(self, ticker: str, df: pd.DataFrame,
                 prior_close: Optional[float] = None) -> Signal:
        if len(df) < MIN_BARS:
            return Signal(ticker, self.name, 0, notes="insufficient_bars")

        row = df.iloc[-1]
        close = float(row["close"])
        atr = row.get("atr")
        if not atr or atr <= 0 or pd.isna(atr):
            return Signal(ticker, self.name, 0, entry_price=close, notes="no_atr")
        atr = float(atr)

        window = df.iloc[-WINDOW:].reset_index(drop=True)
        highs = window["high"].values
        lows = window["low"].values
        closes = window["close"].values
        n = len(window)

        res_levels = _levels(highs, "high")
        sup_levels = _levels(lows, "low")
        if not res_levels or not sup_levels:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="box_no_levels")

        # The box around the CURRENT price: nearest tested floor below,
        # nearest tested ceiling above.
        below = [(lv, t) for lv, t in sup_levels if lv < close]
        above = [(lv, t) for lv, t in res_levels if lv > close]
        if not below or not above:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="box_price_outside")
        support, sup_touches = max(below, key=lambda x: x[0])
        resistance, res_touches = min(above, key=lambda x: x[0])

        height = (resistance - support) / support
        if height < MIN_HEIGHT_PCT:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes=f"box_too_thin({height*100:.1f}%)")
        if height > MAX_HEIGHT_PCT:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes=f"box_too_tall({height*100:.1f}%)")

        # The box is only a box if price RESPECTED it once both levels existed.
        start = max(sup_touches[0], res_touches[0])
        if (n - 1) - start < MIN_BOX_BARS:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes=f"box_too_young({(n - 1) - start}b)")
        body = closes[start:n - 1]          # completed closes only
        if len(body) and (body.max() > resistance * (1 + BREAK_TOL)
                          or body.min() < support * (1 - BREAK_TOL)):
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="box_broken")

        # Entry gate: bottom third of the box only.
        zone_top = support + ENTRY_ZONE * (resistance - support)
        if close > zone_top:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=(f"box_mid(sup={support:.3f},res={resistance:.3f},"
                       f"close={close:.3f})"),
            )

        # ...and price must have actually TAGGED the floor and turned up on
        # this bar, not just drifted along near it.
        tag_low = float(lows[max(0, n - TAG_LOOKBACK):].min())
        tagged = tag_low <= support + TAG_TOL_ATR * atr
        held = close > support
        is_green = close > float(row["open"])
        bounced = close >= tag_low + CONFIRM_ATR * atr
        if not (tagged and held and is_green and bounced):
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=(f"box_no_bounce(sup={support:.3f},low={tag_low:.3f},"
                       f"tag={int(tagged)},grn={int(is_green)},"
                       f"bnc={int(bounced)})"),
            )

        stop = support - STOP_ATR_MULT * atr
        # Apply the v1.6 noise floor HERE too, so the reward test below measures
        # the risk that will actually be traded. main.py/backtest re-apply the
        # same min() afterwards, which is idempotent.
        stop = min(stop, close * (1 - config.MIN_STOP_PCT))
        target = resistance * TARGET_BUFFER
        risk = close - stop
        if risk <= 0:
            return Signal(ticker, self.name, 0, entry_price=close,
                          notes="box_bad_stop")
        if (target - close) < MIN_REWARD_R * risk:
            return Signal(
                ticker, self.name, 0, entry_price=close,
                notes=(f"box_no_room(res={resistance:.3f},"
                       f"r={(target - close) / risk:.2f}R)"),
            )

        return Signal(
            ticker=ticker,
            strategy=self.name,
            score=5,
            entry_price=close,
            notes=(f"box(sup={support:.3f}x{len(sup_touches)},"
                   f"res={resistance:.3f}x{len(res_touches)},"
                   f"h={height*100:.1f}%,r={(target - close) / risk:.2f}R)"),
            stop_price=stop,
            target_price=target,
            take_profit=True,      # sell AT the box top -- that is the strategy
            min_reward_r=MIN_REWARD_R,
            ignore_vwap=IGNORE_VWAP,
        )
