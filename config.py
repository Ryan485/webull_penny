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
SCAN_PRICE_MIN = 0.5
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
STRATEGY_VERSION = "us-penny-v1.4-dbgap60-2026-07-20"
# Max the live quote may sit above the signal price before a buy is skipped
# (anti-chase; FTRK 2026-07-13 filled +4.2% above signal into a spike top).
MAX_ENTRY_CHASE_PCT = float(os.getenv("MAX_ENTRY_CHASE_PCT", 0.015))
LOG_FILE = "logs/trades.log"
STATE_FILE = "logs/state.json"
# Append-only ledger of every LIVE closed trade. Survives the daily state
# reset (state.json is wiped nightly) so real paper-trading outcomes
# accumulate for strategy refinement. Same schema as the backtest trade
# CSVs (date+ticker key) so live and backtest data join against research.
OUTCOMES_FILE = "logs/trade_outcomes.csv"
