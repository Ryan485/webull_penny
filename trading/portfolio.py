"""
In-memory portfolio state with thread-safe access.
Tracks open positions, closed trades, daily P&L, and trade log.
Persists state to JSON so the dashboard can read it.
"""
import csv
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime
from typing import Dict, List, Optional

import pytz

import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")


@dataclass
class Position:
    ticker: str
    strategy: str
    entry_price: float
    stop_price: float
    target_price: float
    shares: int
    entry_time: str
    current_price: float = 0.0
    notes: str = ""
    initial_risk: float = 0.0    # ACTUAL fill - initial stop (R); fixed at entry
    planned_risk: float = 0.0    # pre-order quote - stop; vs initial_risk this
                                 # measures slippage-driven risk overrun
    high_water: float = 0.0      # highest price seen; drives the trailing stop
    take_profit_at_target: bool = False  # sell at target (resistance-capped)

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price


@dataclass
class ClosedTrade:
    ticker: str
    strategy: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str
    exit_reason: str
    notes: str = ""
    planned_risk: float = 0.0   # $ risk at sizing time (pre-fill quote - stop)
    actual_risk: float = 0.0    # $ risk after the fill (fill - stop)


STOP_COOLDOWN_SECS = 1800    # 30 min cooldown after a stop-loss exit
PROFIT_COOLDOWN_SECS = 1800  # 30 min cooldown after a take-profit exit
MAX_TRADES_PER_TICKER_DAY = 3   # hard cap on churn — the binding limit
MAX_STOPS_PER_TICKER_DAY = 3    # raised 2 -> 3 (2026-07-16, TGHL): the 2-stop
                                # blacklist blocked the 14:28 ET rb breakout
                                # (7x vol) that ran 1.46 -> 1.576; sweep on the
                                # full universe: cap 2 PF 2.62/+$25.1K vs cap 3
                                # PF 2.81/+$28.5K. With 3 trades/day the stop
                                # cap now never binds; the trade cap + 30-min
                                # stop cooldown are the churn guards.


class Portfolio:
    def __init__(self):
        self._lock = threading.RLock()  # RLock allows re-entry from same thread
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[ClosedTrade] = []
        self.daily_pnl: float = 0.0
        self.account_value: float = config.ACCOUNT_SIZE
        self.signals_log: List[dict] = []
        self.scanner_results: List[str] = []
        self._stop_cooldowns: Dict[str, float] = {}    # ticker -> timestamp of stop exit
        self._profit_cooldowns: Dict[str, float] = {}  # ticker -> timestamp of profit exit
        self._closing: set = set()   # tickers with a sell in flight (see begin_close)
        self._load_state()
        self._session_date = datetime.now(ET).strftime("%Y-%m-%d")

    def roll_day_if_needed(self) -> None:
        """
        Reset daily P&L/closed trades the moment the ET calendar date
        changes — checked every loop tick instead of waiting for an exact
        hour==0,minute==0 instant, which is easy to miss entirely (laptop
        asleep, process not running overnight) and previously left
        yesterday's trades showing under "today" indefinitely.
        """
        today = datetime.now(ET).strftime("%Y-%m-%d")
        with self._lock:
            if self._session_date == today:
                return
            removed = len(self.closed_trades)
            self.closed_trades = []
            self.daily_pnl = 0.0
            self._stop_cooldowns.clear()
            self._profit_cooldowns.clear()
            self._session_date = today
            self._save_state()
        logger.info(f"Daily rollover to {today}: cleared {removed} closed trade(s)")

    # ── Positions ──────────────────────────────────────────────────────────────

    def add_position(self, pos: Position) -> None:
        with self._lock:
            self.positions[pos.ticker] = pos
            self._save_state()
        logger.info(
            f"ENTER {pos.ticker} @ ${pos.entry_price:.2f} x{pos.shares} "
            f"stop=${pos.stop_price:.2f} tgt=${pos.target_price:.2f} [{pos.strategy}]"
        )
        self._write_log(f"ENTER {pos.ticker} @ {pos.entry_price:.2f} x{pos.shares}")

    def begin_close(self, ticker: str) -> bool:
        """
        Claim a ticker for closing. The exit-monitor thread and the main
        loop's EOD close can otherwise both decide to sell the same position
        in the same instant and double-submit the market sell. Callers must
        call close_position() on success or abort_close() on a failed order.
        """
        with self._lock:
            if ticker not in self.positions or ticker in self._closing:
                return False
            self._closing.add(ticker)
            return True

    def abort_close(self, ticker: str) -> None:
        """Release a begin_close claim after a failed sell order."""
        with self._lock:
            self._closing.discard(ticker)

    def close_position(self, ticker: str, exit_price: float, reason: str) -> Optional[ClosedTrade]:
        with self._lock:
            pos = self.positions.pop(ticker, None)
            self._closing.discard(ticker)
            if pos is None:
                return None
            if reason == "stop_loss":
                self._stop_cooldowns[ticker] = time.time()
            elif reason in ("take_profit", "trailing_stop"):
                self._profit_cooldowns[ticker] = time.time()
            pnl = (exit_price - pos.entry_price) * pos.shares
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            trade = ClosedTrade(
                ticker=ticker,
                strategy=pos.strategy,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                shares=pos.shares,
                pnl=pnl,
                pnl_pct=pnl_pct,
                entry_time=pos.entry_time,
                exit_time=datetime.now(ET).isoformat(),
                exit_reason=reason,
                notes=pos.notes,
                planned_risk=round(pos.planned_risk * pos.shares, 2),
                actual_risk=round(pos.initial_risk * pos.shares, 2),
            )
            self.closed_trades.append(trade)
            self.daily_pnl += pnl
            self._save_state()
        self._log_outcome(trade)
        logger.info(
            f"EXIT {ticker} @ ${exit_price:.2f} ({reason}) "
            f"PnL=${pnl:+.2f} ({pnl_pct:+.1%})"
        )
        self._write_log(
            f"EXIT {ticker} @ {exit_price:.2f} [{reason}] PnL={pnl:+.2f}"
        )
        return trade

    def update_prices(self, prices: Dict[str, float]) -> None:
        with self._lock:
            for ticker, price in prices.items():
                if ticker in self.positions:
                    self.positions[ticker].current_price = price

    def can_open_position(self, ticker: str) -> bool:
        with self._lock:
            if ticker in self.positions:
                return False
            if len(self.positions) >= config.MAX_POSITIONS:
                return False
            stop_ts = self._stop_cooldowns.get(ticker, 0)
            if time.time() < stop_ts + STOP_COOLDOWN_SECS:
                return False
            profit_ts = self._profit_cooldowns.get(ticker, 0)
            if time.time() < profit_ts + PROFIT_COOLDOWN_SECS:
                return False
            trades_today = [t for t in self.closed_trades if t.ticker == ticker]
            if len(trades_today) >= MAX_TRADES_PER_TICKER_DAY:
                return False
            stops_today = [t for t in trades_today if t.exit_reason == "stop_loss"]
            if len(stops_today) >= MAX_STOPS_PER_TICKER_DAY:
                return False
            return True

    def log_signal(self, signal_dict: dict) -> None:
        with self._lock:
            self.signals_log.append(signal_dict)
            if len(self.signals_log) > 200:
                self.signals_log = self.signals_log[-200:]

    def set_scanner_results(self, tickers: List[str]) -> None:
        with self._lock:
            self.scanner_results = tickers
            self._save_state()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "account_value": self.account_value,
                "daily_pnl": self.daily_pnl,
                "positions": {
                    t: {**asdict(p), "unrealized_pnl": p.unrealized_pnl, "pnl_pct": p.pnl_pct}
                    for t, p in self.positions.items()
                },
                "closed_today": [asdict(t) for t in self.closed_trades[-50:]],
                "signals": self.signals_log[-50:],
                "scanner": self.scanner_results,
                "halt": self.daily_pnl < -(self.account_value * config.DAILY_HALT_PCT),
            }

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        os.makedirs("logs", exist_ok=True)
        try:
            snap = self.snapshot()
            snap["date"] = datetime.now(ET).strftime("%Y-%m-%d")
            # Atomic write: dump to a temp file, then rename over the real
            # one. A crash mid-write can no longer leave a torn/invalid
            # state.json (os.replace is atomic on Windows and POSIX).
            tmp = config.STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f, indent=2, default=str)
            os.replace(tmp, config.STATE_FILE)
        except Exception as e:
            logger.warning(f"State save failed: {e}")

    def _load_state(self) -> None:
        if not os.path.exists(config.STATE_FILE):
            return
        try:
            with open(config.STATE_FILE) as f:
                state = json.load(f)
            today = datetime.now(ET).strftime("%Y-%m-%d")
            self.account_value = state.get("account_value", config.ACCOUNT_SIZE)
            self.signals_log = state.get("signals", [])
            self.scanner_results = state.get("scanner", [])
            # Restore only trades whose OWN exit_time is today (ET) — don't
            # trust the file's top-level "date" field alone. A process that
            # stays alive across midnight can re-save yesterday's trades
            # stamped with today's date, which made stale trades look
            # "today" on the dashboard indefinitely (found 2026-07-09).
            valid = {f.name for f in fields(ClosedTrade)}
            todays = []
            for t in state.get("closed_today", []):
                if not str(t.get("exit_time", "")).startswith(today):
                    continue
                try:
                    todays.append(
                        ClosedTrade(**{k: v for k, v in t.items() if k in valid})
                    )
                except Exception:
                    pass
            self.closed_trades = todays
            self.daily_pnl = sum(t.pnl for t in todays)
            # Restore OPEN positions with their full structure (Low2 stop,
            # trailing progress, take-profit flag, planned risk, notes).
            # This was never read back before 2026-07-14 even though
            # _save_state always wrote it — so every mid-session restart
            # fell through to main.py's broker sync, which rebuilds stops
            # from a generic 2%-ATR estimate and loses the real ones.
            # Only same-day entries are restored; anything older is stale
            # (the bot is EOD-flat by design) and is left to the broker sync.
            pos_valid = {f.name for f in fields(Position)}
            for ticker, p in (state.get("positions") or {}).items():
                if not str(p.get("entry_time", "")).startswith(today):
                    continue
                try:
                    self.positions[ticker] = Position(
                        **{k: v for k, v in p.items() if k in pos_valid}
                    )
                    logger.info(f"Restored open position from state: {ticker}")
                except Exception as e:
                    logger.warning(f"Position restore failed for {ticker}: {e}")
        except Exception as e:
            logger.warning(f"State load failed: {e}")

    # First 12 columns match the backtest trade CSVs (backtest_viral_trades.csv)
    # so live and simulated outcomes share one schema and join on date+ticker.
    # The extra columns track execution quality (slippage-driven risk overrun)
    # and which frozen rule-set produced the trade — only trades from the
    # frozen version count toward the formal paper forward test.
    _OUTCOME_COLS = ["date", "ticker", "strategy", "entry_time", "exit_time",
                     "entry", "exit", "shares", "pnl", "pnl_pct", "reason", "notes",
                     "planned_risk", "actual_risk", "risk_overrun_pct", "version"]

    def _log_outcome(self, trade: "ClosedTrade") -> None:
        """
        Append one closed LIVE trade to the persistent outcomes ledger.
        Append-only and never rewritten, so it survives the nightly state
        reset and accumulates a labeled dataset for strategy refinement.
        Best-effort — a logging failure must never break the trade close.
        """
        try:
            os.makedirs("logs", exist_ok=True)
            # Split the ISO timestamps into date + HH:MM to match the
            # backtest CSV format (date column is the join key vs research).
            def _date(iso: str) -> str:
                return str(iso)[:10]

            def _hhmm(iso: str) -> str:
                try:
                    return datetime.fromisoformat(iso).strftime("%H:%M")
                except Exception:
                    return str(iso)[11:16]

            row = {
                "date": _date(trade.entry_time),
                "ticker": trade.ticker,
                "strategy": trade.strategy,
                "entry_time": _hhmm(trade.entry_time),
                "exit_time": _hhmm(trade.exit_time),
                "entry": round(trade.entry_price, 4),
                "exit": round(trade.exit_price, 4),
                "shares": trade.shares,
                "pnl": round(trade.pnl, 2),
                "pnl_pct": round(trade.pnl_pct * 100, 2),
                "reason": trade.exit_reason,
                "notes": trade.notes,
                "planned_risk": trade.planned_risk,
                "actual_risk": trade.actual_risk,
                "risk_overrun_pct": (
                    round(100 * (trade.actual_risk / trade.planned_risk - 1), 1)
                    if trade.planned_risk > 0 else ""
                ),
                "version": config.STRATEGY_VERSION,
            }
            new_file = not os.path.exists(config.OUTCOMES_FILE)
            with open(config.OUTCOMES_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self._OUTCOME_COLS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as e:
            logger.warning(f"Outcome log failed: {e}")

    def _write_log(self, msg: str) -> None:
        os.makedirs("logs", exist_ok=True)
        ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(config.LOG_FILE, "a") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
