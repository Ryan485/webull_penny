"""
Human-in-the-loop control layer.
Dashboard writes pending sell requests; trading loop executes them and records
the outcome to logs/learning.json for future LLM analysis.
"""
import json
import os
from datetime import datetime

import pytz

ET = pytz.timezone("America/New_York")
PENDING_FILE = "logs/pending_sells.json"
LEARNING_FILE = "logs/learning.json"


# ── Live broker/portfolio references (set once at startup) ────────────────────

_broker = None
_portfolio = None


def register(broker, portfolio) -> None:
    global _broker, _portfolio
    _broker = broker
    _portfolio = portfolio


def execute_sell(ticker: str) -> tuple[bool, str]:
    """
    Immediately place a market sell for ticker.
    Returns (success, message).
    """
    if _broker is None or _portfolio is None:
        return False, "Bot not running — restart the bot first"

    pos = _portfolio.positions.get(ticker)
    if pos is None:
        return False, f"No open position for {ticker}"

    try:
        exit_price = _broker.get_quote(ticker) or pos.current_price or pos.entry_price
        ok = _broker.place_market_sell(ticker, pos.shares)
        if not ok:
            return False, f"Broker rejected sell for {ticker}"

        held_mins = (
            datetime.now(ET) -
            datetime.fromisoformat(pos.entry_time).astimezone(ET)
        ).total_seconds() / 60

        record_manual_exit(
            ticker=ticker,
            strategy=pos.strategy,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            bot_target=pos.target_price,
            bot_stop=pos.stop_price,
            shares=pos.shares,
            time_held_mins=held_mins,
            conditions=pos.notes,
        )
        _portfolio.close_position(ticker, exit_price, "manual_sell")
        return True, f"Sold {ticker} @ ${exit_price:.2f} (target was ${pos.target_price:.2f})"
    except Exception as e:
        return False, str(e)


# ── Learning log (records every manual exit for future analysis) ──────────────

def record_manual_exit(ticker: str, strategy: str, entry_price: float,
                       exit_price: float, bot_target: float, bot_stop: float,
                       shares: int, time_held_mins: float, conditions: str) -> None:
    os.makedirs("logs", exist_ok=True)
    data = _read_learning()

    gain_pct = (exit_price - entry_price) / entry_price if entry_price else 0
    target_pct = (bot_target - entry_price) / entry_price if entry_price else 0
    pct_of_target = gain_pct / target_pct if target_pct else 0
    pnl = (exit_price - entry_price) * shares

    record = {
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "time": datetime.now(ET).strftime("%H:%M"),
        "ticker": ticker,
        "strategy": strategy,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "bot_target": round(bot_target, 4),
        "bot_stop": round(bot_stop, 4),
        "shares": shares,
        "pnl": round(pnl, 2),
        "gain_pct": round(gain_pct, 4),
        "target_pct": round(target_pct, 4),
        "pct_of_target_reached": round(pct_of_target, 4),
        "time_held_mins": round(time_held_mins, 1),
        "conditions": conditions,
        "exit_by": "human",
    }

    data["exits"].append(record)

    # Recompute summary stats
    exits = data["exits"]
    if exits:
        gains = [e["gain_pct"] for e in exits]
        pct_of_targets = [e["pct_of_target_reached"] for e in exits]
        data["summary"] = {
            "total_manual_exits": len(exits),
            "avg_gain_pct": round(sum(gains) / len(gains), 4),
            "avg_pct_of_target": round(sum(pct_of_targets) / len(pct_of_targets), 4),
            "avg_time_held_mins": round(
                sum(e["time_held_mins"] for e in exits) / len(exits), 1
            ),
        }

    with open(LEARNING_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _read_learning() -> dict:
    if not os.path.exists(LEARNING_FILE):
        return {"exits": [], "summary": {}}
    try:
        with open(LEARNING_FILE) as f:
            return json.load(f)
    except Exception:
        return {"exits": [], "summary": {}}


def get_learning_summary() -> dict:
    return _read_learning().get("summary", {})
