import os
from dotenv import load_dotenv

load_dotenv()

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
STRATEGY_VERSION = "us-penny-v1.5-pricefloor1-2026-07-21"
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
