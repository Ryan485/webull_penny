"""
CashCow Penny Bot — main entry point.
Runs the trading loop (10-second checks) and the dashboard in parallel threads.

Usage:
    python main.py              # live / paper trading + dashboard
    python main.py --no-dash    # trading only, no dashboard
    python main.py --dash-only  # dashboard only (no trading)
"""
import argparse
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime

import pytz

import config

# Cap every network call (yfinance, Alpaca, etc.) at 15 seconds.
# Without this, a single hung HTTP request blocks the entire trading loop.
socket.setdefaulttimeout(15)
from data.indicators import compute_all
from data.market_data import get_live_bars, get_prior_close, is_market_open
from data.research import get_catalyst
from data.scanner import get_watchlist
from strategies.double_bottom import DoubleBottom
from strategies.trend_reversal import TrendReversal
from strategies.resistance_breakout import ResistanceBreakout
from trading.broker import create_broker
from trading.portfolio import Portfolio, Position
from trading.risk_manager import (
    calc_stop_and_target, calc_position_size, daily_halt_triggered
)

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

STRATEGIES = [DoubleBottom(), TrendReversal(), ResistanceBreakout()]

# No entries in the first 30 min (open whipsaw) — losses clustered 9:47-10:04
ENTRY_START_HOUR, ENTRY_START_MIN = 10, 0


# ── Scanner loop (runs every SCAN_INTERVAL_MINS minutes) ─────────────────────

class BotState:
    def __init__(self):
        self.watchlist: list = []
        self.prior_closes: dict = {}
        self.lock = threading.Lock()


def scanner_loop(state: BotState, portfolio: Portfolio, stop_event: threading.Event):
    """Refresh the viral stock watchlist every 5 minutes."""
    while not stop_event.is_set():
        try:
            tickers = get_watchlist()
            with state.lock:
                state.watchlist = tickers
                state.prior_closes = {}  # clear cache on new scan
            portfolio.set_scanner_results(tickers)
            logger.info(f"Scanner updated: {len(tickers)} tickers in watchlist")
        except Exception as e:
            logger.error(f"Scanner error: {e}")
        stop_event.wait(config.SCAN_INTERVAL_MINS * 60)


# ── Position exit monitor ─────────────────────────────────────────────────────

def check_exits(portfolio: Portfolio, broker) -> None:
    """
    Monitor open positions with a trailing-stop system:
      - Initial stop as set at entry
      - At +1R unrealized: stop trails 0.75R below the high-water mark
        (starts at entry + 0.25R, so breakeven is covered immediately).
        Trail used to start at +1.5R with only breakeven in between; a
        winner topping out at +1R..+1.5R round-tripped to zero
        (SOXS 2026-07-07: +5.1% peak gave back everything).
    No fixed take-profit — winners run until the trail catches them.
    """
    for ticker, pos in list(portfolio.positions.items()):
        try:
            price = broker.get_quote(ticker)
            if price is None:
                continue
            portfolio.update_prices({ticker: price})

            entry = pos.entry_price
            pos.high_water = max(pos.high_water or entry, price)
            r = pos.initial_risk or (entry - pos.stop_price)

            if r > 0 and pos.high_water >= entry + r:
                trail = round(pos.high_water - 0.75 * r, 4)
                if trail > pos.stop_price:
                    if pos.stop_price < entry <= trail:
                        logger.info(
                            f"{ticker}: +1R reached - trailing stop armed at ${trail:.2f}"
                        )
                    pos.stop_price = trail

            if price <= pos.stop_price:
                reason = "trailing_stop" if pos.stop_price >= entry else "stop_loss"
                if not portfolio.begin_close(ticker):
                    continue  # another thread is already selling this one
                fill = broker.place_market_sell(ticker, pos.shares)
                if fill:
                    actual_exit = fill if isinstance(fill, float) else pos.stop_price
                    portfolio.close_position(ticker, actual_exit, reason)
                else:
                    portfolio.abort_close(ticker)
            elif pos.take_profit_at_target and pos.target_price \
                    and price >= pos.target_price:
                # Resistance-capped target: sell into the wall
                if not portfolio.begin_close(ticker):
                    continue
                fill = broker.place_market_sell(ticker, pos.shares)
                if fill:
                    actual_exit = fill if isinstance(fill, float) else pos.target_price
                    portfolio.close_position(ticker, actual_exit, "take_profit")
                else:
                    portfolio.abort_close(ticker)
        except Exception as e:
            logger.error(f"Exit check error for {ticker}: {e}")


# ── Entry scanner ─────────────────────────────────────────────────────────────

def check_entries(state: BotState, portfolio: Portfolio, broker) -> None:
    """Evaluate every watchlist ticker for entry signals."""
    # Time-of-day gate: skip the opening whipsaw
    now_et = datetime.now(ET)
    if (now_et.hour, now_et.minute) < (ENTRY_START_HOUR, ENTRY_START_MIN):
        return

    with state.lock:
        tickers = list(state.watchlist)
        prior_closes_cache = state.prior_closes

    logger.debug(f"check_entries: {len(tickers)} tickers")

    for ticker in tickers:
        # ALL per-ticker work stays inside this try: an exception escaping
        # here would kill the whole scan pass (and, without the boundary in
        # trading_loop, the trading thread itself).
        try:
            if not portfolio.can_open_position(ticker):
                continue
            # Catalyst gate: no story + no social buzz = pump-and-dump risk
            if get_catalyst(ticker) == "unknown":
                continue
            df = get_live_bars(ticker)
            if df is None or len(df) < 50:
                continue
            df = compute_all(df)

            # VWAP regime filter: only long when buyers are in control.
            # Applied per-signal below so early W-bottom entries (which are
            # below VWAP by nature) can waive it via Signal.ignore_vwap.
            last_row = df.iloc[-1]
            vwap = last_row.get("vwap")
            below_vwap = bool(
                vwap == vwap and vwap and last_row["close"] < vwap  # NaN-safe
            )

            # Get prior close (cached per scan cycle)
            if ticker not in prior_closes_cache:
                prior_closes_cache[ticker] = get_prior_close(ticker)
            prior_close = prior_closes_cache.get(ticker)

            best = None
            for strat in STRATEGIES:
                sig = strat.evaluate(ticker, df, prior_close=prior_close)
                now_str = datetime.now(ET).strftime("%H:%M:%S")
                portfolio.log_signal({
                    "time": now_str,
                    "ticker": ticker,
                    "strategy": sig.strategy,
                    "score": sig.score,
                    "notes": sig.notes,
                })
                if sig.triggered:
                    if below_vwap and not sig.ignore_vwap:
                        continue
                    if best is None or sig.score > best.score:
                        best = sig

            if best is None:
                continue

            logger.info(f"SIGNAL {ticker}: {best.strategy} score={best.score} entry={best.entry_price:.4f}")

            # Confirm price from broker (more accurate than last candle close)
            live_price = broker.get_quote(ticker) or best.entry_price
            # Chase guard (added 2026-07-13, FTRK): a market order placed into
            # a vertical spike fills way above the signal (FTRK 14:29: signal
            # 0.6332, fill 0.66 = +4.2%, then it collapsed for -6.9%). If the
            # live quote has already run more than MAX_ENTRY_CHASE_PCT above
            # the signal bar's price, the entry is gone — skip, don't chase.
            if live_price > best.entry_price * (1 + config.MAX_ENTRY_CHASE_PCT):
                logger.info(
                    f"SKIP {ticker}: price ran {live_price:.4f} vs signal "
                    f"{best.entry_price:.4f} (+{(live_price/best.entry_price-1)*100:.1f}%), not chasing"
                )
                continue
            row = df.iloc[-1]
            atr = row.get("atr")
            if not atr or atr <= 0:
                continue

            if best.stop_price is not None and best.target_price is not None:
                stop, target = best.stop_price, best.target_price
            else:
                stop, target = calc_stop_and_target(live_price, atr)
            account_val = broker.get_account_value()
            shares = calc_position_size(account_val, live_price, stop)

            if shares < 1:
                continue

            fill = broker.place_market_buy(ticker, shares)
            if fill:
                actual_entry = fill if isinstance(fill, float) else live_price
                pos = Position(
                    ticker=ticker,
                    strategy=best.strategy,
                    entry_price=actual_entry,
                    stop_price=stop,
                    target_price=target,
                    shares=shares,
                    entry_time=datetime.now(ET).isoformat(),
                    current_price=live_price,
                    notes=best.notes,
                    initial_risk=max(actual_entry - stop, 0.0),
                    planned_risk=max(live_price - stop, 0.0),
                    high_water=actual_entry,
                    take_profit_at_target=best.take_profit,
                )
                portfolio.add_position(pos)

        except Exception as e:
            logger.error(f"Entry check error for {ticker}: {e}")


# ── Market close cleanup ──────────────────────────────────────────────────────

def eod_close_all(portfolio: Portfolio, broker) -> None:
    """Force-close all positions at EOD. Caller retries while any remain."""
    for ticker, pos in list(portfolio.positions.items()):
        try:
            if not portfolio.begin_close(ticker):
                continue  # exit thread is already selling this one
            price = broker.get_quote(ticker) or pos.entry_price
            fill = broker.place_market_sell(ticker, pos.shares)
            if fill:
                actual_exit = fill if isinstance(fill, float) else price
                portfolio.close_position(ticker, actual_exit, "eod_close")
                logger.info(f"EOD close: {ticker}")
            else:
                portfolio.abort_close(ticker)
                logger.error(f"EOD close: sell order failed for {ticker}")
        except Exception as e:
            portfolio.abort_close(ticker)
            logger.error(f"EOD close error for {ticker}: {e}")


# ── Main trading loop ─────────────────────────────────────────────────────────

def exit_monitor_loop(portfolio: Portfolio, broker, stop_event: threading.Event):
    """
    Dedicated stop/target watcher, independent of entry scanning.
    Stops are client-side, so they must never wait behind a slow watchlist
    scan (each ticker in check_entries does several network calls with 15s
    timeouts; a few slow ones used to delay the next exit check by minutes).
    Runs every EXIT_CHECK_SECS while the market is open.
    """
    while not stop_event.is_set():
        try:
            if is_market_open() and portfolio.positions:
                check_exits(portfolio, broker)
        except Exception as e:
            logger.error(f"Exit monitor error: {e}")
        stop_event.wait(EXIT_CHECK_SECS)


EXIT_CHECK_SECS = 5


def trading_loop(broker, portfolio: Portfolio, stop_event: threading.Event,
                 exit_after_close: bool = False):
    state = BotState()

    # Initial scan
    scanner_thread = threading.Thread(
        target=scanner_loop, args=(state, portfolio, stop_event), daemon=True
    )
    scanner_thread.start()

    # Exits get their own thread — see exit_monitor_loop docstring
    exit_thread = threading.Thread(
        target=exit_monitor_loop, args=(portfolio, broker, stop_event), daemon=True
    )
    exit_thread.start()

    # Wait for first scan
    time.sleep(5)

    eod_closed = False
    last_balance_sync = 0
    _halt_logged = False

    while not stop_event.is_set():
        now_et = datetime.now(ET)

        # Reset daily state the moment the ET date changes (checked every
        # tick, not just at an exact hour==0/minute==0 instant — that instant
        # is easy to miss if the laptop is asleep or the bot isn't running).
        prev_session_date = portfolio._session_date
        portfolio.roll_day_if_needed()
        if portfolio._session_date != prev_session_date:
            eod_closed = False
            _halt_logged = False

        if not is_market_open():
            # Scheduled-run mode: once the session is over (or on a holiday
            # when it never opens), shut down so the laptop can sleep.
            if exit_after_close and now_et.hour >= 16:
                logger.info("Exit-after-close: market closed, shutting down")
                stop_event.set()
                break
            eod_closed = False
            stop_event.wait(30)
            continue

        # EOD position cleanup (3:30 PM ET onwards — 30 min before close).
        # RETRIES every tick until the portfolio is confirmed flat: a single
        # failed sell used to be logged and forgotten (eod_closed was set
        # unconditionally), leaving an unintended overnight position in a
        # volatile penny stock. Found in external review 2026-07-14.
        eod_window = now_et.hour > 15 or (now_et.hour == 15 and now_et.minute >= 30)
        if eod_window and not eod_closed:
            if portfolio.positions:
                eod_close_all(portfolio, broker)
            if not portfolio.positions:
                eod_closed = True
                try:
                    from reporting.daily_report import save_report
                    path = save_report(portfolio)
                    logger.info(f"Daily report saved: {path}")
                except Exception as e:
                    logger.warning(f"Report generation failed: {e}")
                # Scheduled-run mode: done for the day once everything is flat.
                if exit_after_close:
                    logger.info("Exit-after-close: EOD done, shutting down")
                    stop_event.set()
                    break
            else:
                logger.warning(
                    f"EOD close incomplete, {len(portfolio.positions)} position(s) "
                    f"remain - retrying next tick"
                )

        # (Exit monitoring runs in its own thread — exit_monitor_loop)
        halt = config.DAILY_HALT_ENABLED and daily_halt_triggered(portfolio.daily_pnl, portfolio.account_value)
        if halt:
            if not _halt_logged:
                logger.warning(
                    f"DAILY HALT triggered. P&L=${portfolio.daily_pnl:.2f} "
                    f"({portfolio.daily_pnl/portfolio.account_value:.1%}) — no new entries until tomorrow."
                )
                _halt_logged = True
            portfolio._save_state()
            stop_event.wait(config.CHECK_INTERVAL_SECS)
            continue

        # Sync account balance every 60 seconds
        if time.time() - last_balance_sync > 60:
            try:
                real_balance = broker.get_account_value()
                if real_balance and real_balance > 0:
                    portfolio.account_value = real_balance
            except Exception:
                pass
            last_balance_sync = time.time()

        # Look for new entries — but not within the EOD window.
        # Boundary so no unexpected error can kill the trading thread while
        # the dashboard keeps the process looking alive.
        if not eod_window and len(portfolio.positions) < config.MAX_POSITIONS:
            try:
                check_entries(state, portfolio, broker)
            except Exception as e:
                logger.error(f"check_entries failed: {e}")

        # Persist snapshot so dashboard sees latest signals/scanner results
        portfolio._save_state()

        stop_event.wait(config.CHECK_INTERVAL_SECS)


# ── Entry point ───────────────────────────────────────────────────────────────

SINGLETON_PORT = 8049  # localhost mutex; a second bind fails while a bot runs


def acquire_single_instance_lock():
    """
    Bind an exclusive localhost port as a cross-process mutex. Two bot
    instances trading the same account double-buy tickers (BIYA 2026-07-08:
    a stale manual run + the 6:30 scheduled run each opened a position).
    The OS releases the port automatically if the process dies.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description="CashCow Penny Bot")
    parser.add_argument("--no-dash", action="store_true", help="Skip dashboard")
    parser.add_argument("--dash-only", action="store_true", help="Dashboard only, no trading")
    parser.add_argument("--exit-after-close", action="store_true",
                        help="Shut down after the EOD close/report (scheduled runs)")
    args = parser.parse_args()

    if not args.dash_only:
        instance_lock = acquire_single_instance_lock()
        if instance_lock is None:
            logger.error(
                "Another bot instance is already running (port %d bound) - exiting. "
                "Stop it first if you meant to restart.", SINGLETON_PORT
            )
            return

    portfolio = Portfolio()
    stop_event = threading.Event()

    threads = []

    if not args.dash_only:
        try:
            broker = create_broker()
        except Exception as e:
            logger.error(f"Broker init failed: {e}")
            logger.info("Falling back to Paper broker")
            from trading.broker import PaperBroker
            broker = PaperBroker()

        from trading.manual_control import register as register_manual
        register_manual(broker, portfolio)

        # Sync real account balance from broker at startup
        try:
            real_balance = broker.get_account_value()
            if real_balance and real_balance > 0:
                portfolio.account_value = real_balance
                logger.info(f"Account balance synced: ${real_balance:,.2f}")
        except Exception as e:
            logger.warning(f"Could not sync account balance: {e}")

        # Sync open positions from broker so the bot doesn't re-buy on restart
        if hasattr(broker, "get_open_positions"):
            try:
                open_pos = broker.get_open_positions()
                for p in open_pos:
                    ticker = p["ticker"]
                    if ticker not in portfolio.positions:
                        from trading.risk_manager import calc_stop_and_target
                        cost = p["cost_price"]
                        last = p["last_price"]
                        # Estimate ATR from 2% of price as a safe default
                        atr_est = cost * 0.02
                        stop, target = calc_stop_and_target(cost, atr_est)
                        pos = Position(
                            ticker=ticker,
                            strategy="synced_from_broker",
                            entry_price=cost,
                            stop_price=stop,
                            target_price=target,
                            shares=p["shares"],
                            entry_time=datetime.now(ET).isoformat(),
                            current_price=last,
                            initial_risk=max(cost - stop, 0.0),
                            high_water=max(cost, last),
                        )
                        portfolio.positions[ticker] = pos
                        logger.info(
                            f"Synced existing position: {ticker} x{p['shares']} "
                            f"@ ${cost:.2f} from Webull"
                        )
                if open_pos:
                    portfolio._save_state()
            except Exception as e:
                logger.warning(f"Position sync error: {e}")

        t = threading.Thread(
            target=trading_loop,
            args=(broker, portfolio, stop_event, args.exit_after_close),
            daemon=True,
        )
        t.start()
        threads.append(t)
        logger.info(
            f"Trading loop started | broker={config.BROKER}, "
            f"account=${portfolio.account_value:,.2f}"
        )

    if not args.no_dash:
        from dashboard.app import run_dashboard
        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()
        threads.append(dash_thread)
        logger.info(f"Dashboard: http://localhost:{config.DASHBOARD_PORT}")

    try:
        while not stop_event.is_set():
            time.sleep(1)
        logger.info("Stop event set - exiting")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        stop_event.set()


if __name__ == "__main__":
    main()
