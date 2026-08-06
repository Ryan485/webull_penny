import math
import os
from dotenv import load_dotenv

load_dotenv()


def _env_fraction(name: str, default: float, lo: float, hi: float) -> float:
    """Read a fractional env override, failing CLOSED on nonsense values.

    A risk parameter that silently accepts NaN, inf, a negative, or an absurd
    magnitude is worse than no override at all: config.MIN_STOP_PCT is what keeps
    the live stop strictly below entry, so a bad value there can put a real order
    on the book with a stop at/above entry or no effective stop. Refuse to start
    instead of trading on it.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={raw!r} is not a number")
    if not math.isfinite(val):
        raise ValueError(f"{name}={raw!r} must be finite")
    if not (lo <= val <= hi):
        raise ValueError(f"{name}={val} is outside the safe range [{lo}, {hi}]")
    return val


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Read an integer env override, failing CLOSED on nonsense values.

    Same rationale as _env_fraction: a strategy gate expressed as a bar count
    or touch count (e.g. box_range's BOX_MIN_TOUCHES) is a defining constraint,
    not decoration -- an unvalidated override of 0 can silently disable it.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={raw!r} is not an integer")
    if not (lo <= val <= hi):
        raise ValueError(f"{name}={val} is outside the safe range [{lo}, {hi}]")
    return val

# --- Alpaca ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# --- Webull ---
WEBULL_EMAIL = os.getenv("WEBULL_EMAIL", "")
WEBULL_PASSWORD = os.getenv("WEBULL_PASSWORD", "")
WEBULL_TRADING_PIN = os.getenv("WEBULL_TRADING_PIN", "")
WEBULL_DEVICE_ID = os.getenv("WEBULL_DEVICE_ID", "")
# Token-based auth (from capture_webull_token.py — preferred, no MFA needed)
WEBULL_ACCESS_TOKEN = os.getenv("WEBULL_ACCESS_TOKEN", "")
WEBULL_REFRESH_TOKEN = os.getenv("WEBULL_REFRESH_TOKEN", "")
WEBULL_TOKEN_EXPIRE = os.getenv("WEBULL_TOKEN_EXPIRE", "")
WEBULL_UUID = os.getenv("WEBULL_UUID", "")

# --- Active broker: "webull" or "alpaca" ---
BROKER = os.getenv("BROKER", "alpaca")

# --- Risk / sizing ---
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", 10000))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", 3))
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", 0.02))
DAILY_HALT_PCT = float(os.getenv("DAILY_HALT_PCT", 0.06))
DAILY_HALT_ENABLED = os.getenv("DAILY_HALT_ENABLED", "true").lower() != "false"
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", 3))

# --- Active strategies ---
# Which strategies main.py, backtest_viral.py and debug_entries.py actually
# build (see strategies/registry.py). Turning one OFF here does not delete it --
# the class stays registered, it just is not instantiated, so an experiment can
# be run and reverted without touching strategy code.
# Default is the frozen v1.6 set. Box-range (박스권매매) experiment 2026-08-04:
#   ENABLED_STRATEGIES=box_range py -3.12 backtest_viral.py
_DEFAULT_STRATEGIES = "double_bottom,trend_reversal,resistance_breakout"
ENABLED_STRATEGIES = os.getenv("ENABLED_STRATEGIES", _DEFAULT_STRATEGIES)

# --- Strategy parameters ---
CANDLE_INTERVAL = "1Min"
CHECK_INTERVAL_SECS = 10
SCAN_INTERVAL_MINS = 5

RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
STOCH_FAST = (5, 3, 3)
STOCH_MED = (10, 5, 5)
MA_SHORT, MA_MID = 5, 10
ATR_PERIOD = 14
AVG_VOL_PERIOD = 20

# ATR stop multiplier and hard floor
ATR_STOP_MULT = 1.5
MAX_STOP_PCT = 0.05
# Minimum stop distance as a fraction of entry (v1.6, 2026-07-24).
# The structural stops (pattern low - N*ATR) came out at a MEDIAN of 1.53% of
# entry, while the median dip a live trade had to survive before running was
# 3.9% -- i.e. the stop sat INSIDE normal noise and 67% of stopped-out trades
# recovered above our entry (6 of 18 went on to reach the full 2R target).
# Root cause is partly the data feed: IEX ATR understates true volatility on
# thin names (ATAI 0.22% ATR -> a 0.60% stop), so a percentage floor is the
# right compensation for a known feed bias, not a curve-fit. Applied in main.py
# BEFORE position sizing, so a wider stop simply buys fewer shares: the dollar
# risk per trade is unchanged and commissions DROP (fewer shares).
# NOTE: at 3.0% this floor binds on ~89% of the observed live trades, so in
# practice the stop is "3% of entry, or the structural stop when that is wider".
# VALIDATED (2026-07-29, Codex R1-INVALID-STOP): an unvalidated env override could
# set this to 0 (clamp yields stop == entry), NaN (clamp silently skipped, since
# `stop > nan` is False), or >1 (negative stop that never triggers). Each of those
# put a real buy on the book with no working protective exit. Range capped at 0.50
# because MAX_STOP_PCT is 0.05 and anything near 1.0 is meaningless. 0.0 is still
# permitted so the floor can be disabled deliberately -- main.py now carries an
# explicit stop-below-entry guard, so disabling it is no longer unsafe.
MIN_STOP_PCT = _env_fraction("MIN_STOP_PCT", 0.03, 0.0, 0.50)

# Take profit: max(2R, entry * 1.10)
TAKE_PROFIT_R = 2.0
TAKE_PROFIT_MIN_PCT = 0.10

# --- Scanner filters ---
# Raised 0.5 -> 1.0 on 2026-07-21 (v1.5) to sidestep the IBKR per-share fee tax:
# sub-$1 names pay ~1.5% round-trip commission PLUS the widest spreads, and the
# sim that showed them net-positive models zero spread + has survivorship bias,
# so their true edge is the least trustworthy. The profitable $1-2 bucket stays.
# See the fee/price-floor entry in webull_penny\CLAUDE.md (KIDZ 2026-07-21).
SCAN_PRICE_MIN = 1.0
SCAN_PRICE_MAX = 10.0
SCAN_REL_VOL_MIN = 3.0
SCAN_MIN_VOLUME = 300_000

# --- Market hours (Eastern Time) ---
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
STRATEGY1_CLOSE = "10:30"

# --- Dashboard ---
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 8050))
# Frozen rule-set tag stamped on every logged trade. Bump ONLY when strategy
# logic/parameters change; trades from one version form one forward-test
# sample and must not be mixed with trades from another.
STRATEGY_VERSION = "us-penny-v1.7-rewardguard-2026-08-05"


def _normalize_strategy_spec(spec: str) -> str:
    """Dedupe/trim/order-preserve, mirroring strategies.registry.parse_spec's
    normalization (not imported directly: registry imports config, and
    importing it back here would be circular). Used only for identity
    comparison, not validation -- an invalid spec still fails loudly in
    build_strategies(), this just must not let two equivalent-but-differently
    -formatted specs diverge, or two different specs collide."""
    seen, ordered = set(), []
    for n in spec.split(","):
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    return ",".join(ordered)


# box_range's own tunable knobs (Codex NEW-BOX-PARAM-VERSION-MIX, 2026-08-04):
# knowing box_range is active is not enough to keep forward-test samples
# separated -- BOX_ENTRY_ZONE=0.33 and BOX_ENTRY_ZONE=1.0 fire on materially
# different entries but both only said "+strategies:box_range".


# The TRUE compiled-in default for each box_range env var, duplicated here
# (box_range.py cannot be imported back -- same circularity as
# _normalize_strategy_spec) so the fingerprint below can compare EFFECTIVE
# values, not just "was something typed into the env". Codex round 3
# (2026-08-04) correctly rejected the earlier "present and non-blank" version:
# an unset BOX_ENTRY_ZONE and an explicit BOX_ENTRY_ZONE=0.33 (its own
# default) produced DIFFERENT tags, violating "unchanged defaults stay
# stable". MUST be kept in sync with strategies/box_range.py's actual
# defaults -- tests/test_box_range.py asserts the two never drift apart.
_BOX_DEFAULTS = {
    "BOX_MIN_TOUCHES": (2, int), "BOX_MIN_HEIGHT_PCT": (0.04, float),
    "BOX_MAX_HEIGHT_PCT": (0.20, float), "BOX_MIN_BARS": (30, int),
    "BOX_ENTRY_ZONE": (0.33, float), "BOX_STOP_ATR_MULT": (0.5, float),
    "BOX_MIN_REWARD_R": (1.0, float), "BOX_IGNORE_VWAP": (True, bool),
}


def _box_param_fingerprint() -> str:
    """Sorted "VAR=value" list of every BOX_* env var whose EFFECTIVE value
    differs from its true default -- not merely "is the var present". Emits
    the CANONICAL value (Codex round 5, 2026-08-04: emitting the raw text
    instead meant BOX_ENTRY_ZONE=.5 and BOX_ENTRY_ZONE=0.50 -- the same
    effective float -- produced different fingerprints and therefore
    different OUTCOME_VERSION tags, despite being genuinely equivalent
    configurations). An unparseable override (which box_range.py itself will
    refuse to start on) still counts as a difference, using its raw text
    since there is no canonical value to derive -- that configuration fails
    to construct BoxRange() regardless, so its exact fingerprint text has no
    operational consequence. Two runs with an identically-valued override
    always produce the identical fingerprint, so under-differentiation (the
    actual harm the finding is about) remains impossible regardless of
    parsing edge cases.

    BOX_IGNORE_VWAP is handled separately from the numeric knobs (Codex round
    5, 2026-08-05): box_range.py reads it as a plain, UNSTRIPPED
    `os.environ.get(name, "1") == "1"` -- a present-but-blank or whitespace-
    padded value is NOT the same as an absent one there, unlike the numeric
    readers (_env_fraction/_env_int), which treat blank-after-strip as
    "absent, use default". Stripping/blank-skipping this one the same way as
    the numeric knobs let two configs with genuinely different real
    IGNORE_VWAP behavior (unset vs " 1 ": True vs False at runtime) collapse
    onto the identical fingerprint. This branch mirrors box_range.py's exact
    expression instead of reusing the numeric path's normalization."""
    overrides = []
    for name, (default, kind) in _BOX_DEFAULTS.items():
        if kind is bool:
            present = os.environ.get(name)
            if present is None:
                continue   # truly absent -- matches the default, no entry
            effective = (present == "1")
            if effective != default:
                overrides.append(f"{name}={'1' if effective else '0'}")
            continue
        raw = os.environ.get(name, "").strip()
        if raw == "":
            continue
        try:
            effective = kind(raw)
        except (TypeError, ValueError):
            overrides.append(f"{name}={raw}")
            continue
        if effective != default:
            overrides.append(f"{name}={repr(effective)}")
    return ",".join(sorted(overrides))


# Actually stamped on every logged trade (Codex NEW-STRATEGY-VERSION-MIX /
# NEW-BOX-PARAM-VERSION-MIX, 2026-08-04): a non-default ENABLED_STRATEGIES, or
# a non-default box_range parameter while it is active, changes which signals
# fire, so those trades must not silently join the frozen v1.6 forward-test
# sample under the same tag. Identical to STRATEGY_VERSION only when the
# active set matches the default AND no box_range override is set.
_normalized_enabled = _normalize_strategy_spec(ENABLED_STRATEGIES)
_outcome_suffixes = []
if _normalized_enabled != _normalize_strategy_spec(_DEFAULT_STRATEGIES):
    _outcome_suffixes.append(f"strategies:{_normalized_enabled}")
if "box_range" in _normalized_enabled.split(","):
    _box_fp = _box_param_fingerprint()
    if _box_fp:
        _outcome_suffixes.append(f"box:{_box_fp}")
OUTCOME_VERSION = (
    STRATEGY_VERSION if not _outcome_suffixes
    else STRATEGY_VERSION + "+" + "+".join(_outcome_suffixes)
)
# Max the live quote may sit above the signal price before a buy is skipped
# (anti-chase; FTRK 2026-07-13 filled +4.2% above signal into a spike top).
MAX_ENTRY_CHASE_PCT = float(os.getenv("MAX_ENTRY_CHASE_PCT", 0.015))

# --- Broker commissions (IBKR, US stocks, Fixed tier) ---
# Owner trades US stocks from Canada through IBKR at go-live; model the cost
# NOW so paper P&L is net-of-fees and matches what live will actually clear.
# IBKR Fixed US-stock schedule (USD): $0.005/share, $1.00 minimum per order,
# capped at 1.0% of trade value. For a penny bot buying 10k-20k shares this is
# the dominant cost: at ~$0.50 the per-share rate IS ~1% of value per side, so
# a round trip can eat ~2% -- larger than the strategy's ~0.75% edge. Higher-
# priced names ($5+) pay ~0.1%/side and are unaffected. Applied per side (entry
# AND exit) in Portfolio.close_position.
IBKR_PER_SHARE = float(os.getenv("IBKR_PER_SHARE", 0.005))     # USD / share
IBKR_MIN_PER_ORDER = float(os.getenv("IBKR_MIN_PER_ORDER", 1.00))  # USD / order
IBKR_MAX_PCT = float(os.getenv("IBKR_MAX_PCT", 0.01))          # cap: 1% of value


def ibkr_commission(shares: int, price: float) -> float:
    """One-side IBKR Fixed-tier US-stock commission in USD."""
    if shares <= 0 or price <= 0:
        return 0.0
    comm = shares * IBKR_PER_SHARE
    comm = min(comm, IBKR_MAX_PCT * shares * price)   # 1% of value cap
    comm = max(comm, IBKR_MIN_PER_ORDER)              # $1 floor
    return round(comm, 4)
LOG_FILE = "logs/trades.log"
STATE_FILE = "logs/state.json"
# Append-only ledger of every LIVE closed trade. Survives the daily state
# reset (state.json is wiped nightly) so real paper-trading outcomes
# accumulate for strategy refinement. Same schema as the backtest trade
# CSVs (date+ticker key) so live and backtest data join against research.
OUTCOMES_FILE = "logs/trade_outcomes.csv"
