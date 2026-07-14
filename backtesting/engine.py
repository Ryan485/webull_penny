"""
Backtesting engine.
Replays historical 1-minute bars against all four strategies.
Supports the full viral stock universe (VIRAL_WATCHLIST + custom tickers).

Usage:
    python backtest_runner.py --tickers AMC GME BBBY --start 2023-01-01 --end 2023-12-31
    python backtest_runner.py --universe viral --start 2024-01-01 --end 2024-06-01
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import numpy as np
import pytz

import config
from data.indicators import compute_all
from data.market_data import get_historical_bars, get_bars_yfinance
from data.scanner import VIRAL_WATCHLIST
from strategies.gap_bounce import GapBounce
from strategies.double_bottom import DoubleBottom
from strategies.trend_reversal import TrendReversal
from strategies.resistance_breakout import ResistanceBreakout
from trading.risk_manager import calc_stop_and_target, calc_position_size

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

STRATEGIES = [GapBounce(), DoubleBottom(), TrendReversal(), ResistanceBreakout()]


@dataclass
class BacktestTrade:
    ticker: str
    strategy: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    score: int
    notes: str


@dataclass
class BacktestResult:
    tickers: List[str]
    start: str
    end: str
    trades: List[BacktestTrade] = field(default_factory=list)
    initial_capital: float = config.ACCOUNT_SIZE

    # ── Computed metrics ──────────────────────────────────────────────────────

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.pnl > 0]

    @property
    def losses(self) -> List[BacktestTrade]:
        return [t for t in self.trades if t.pnl <= 0]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len(self.wins) / len(self.trades)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_win(self) -> float:
        return np.mean([t.pnl for t in self.wins]) if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return np.mean([t.pnl for t in self.losses]) if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.wins)
        gross_loss = abs(sum(t.pnl for t in self.losses))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        equity = self.initial_capital
        peak = equity
        max_dd = 0.0
        for t in self.trades:
            equity += t.pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
        return max_dd

    def summary(self) -> str:
        lines = [
            f"\n{'='*60}",
            f"  BACKTEST RESULTS",
            f"  Period  : {self.start} → {self.end}",
            f"  Tickers : {len(self.tickers)} stocks",
            f"{'='*60}",
            f"  Trades      : {self.total_trades}",
            f"  Win Rate    : {self.win_rate:.1%}",
            f"  Total PnL   : ${self.total_pnl:+,.2f}",
            f"  Avg Win     : ${self.avg_win:+.2f}",
            f"  Avg Loss    : ${self.avg_loss:+.2f}",
            f"  Profit Factor: {self.profit_factor:.2f}",
            f"  Max Drawdown : {self.max_drawdown:.1%}",
            f"{'='*60}\n",
        ]
        return "\n".join(lines)

    def to_dataframe(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])


# ── Core runner ───────────────────────────────────────────────────────────────

def _is_morning_window(ts: pd.Timestamp) -> bool:
    t = ts.tz_convert(ET) if ts.tzinfo else ts
    return t.hour == 9 and t.minute >= 30 or (t.hour == 10 and t.minute < 30)


def _run_ticker(ticker: str, df_day: pd.DataFrame,
                result: BacktestResult, account_value: float) -> float:
    """Simulate one ticker for one trading day. Returns realized PnL."""
    if df_day is None or len(df_day) < 50:
        return 0.0

    df_day = compute_all(df_day.copy())
    pnl = 0.0
    position = None  # (entry_price, stop, target, shares, strategy_name, score, notes, entry_ts)

    for i in range(50, len(df_day)):
        window = df_day.iloc[: i + 1]
        row = window.iloc[-1]
        ts = window.index[-1]
        price = row["close"]

        # Exit logic
        if position:
            ep, stop, target, shares, strat, score, notes, entry_ts = position
            reason = None
            exit_price = price

            if price <= stop:
                reason = "stop_loss"
                exit_price = stop
            elif price >= target:
                reason = "take_profit"
                exit_price = target
            elif ts.hour >= 15 and ts.minute >= 55:
                reason = "eod_close"

            if reason:
                trade_pnl = (exit_price - ep) * shares
                pnl += trade_pnl
                result.trades.append(BacktestTrade(
                    ticker=ticker,
                    strategy=strat,
                    entry_time=str(entry_ts),
                    exit_time=str(ts),
                    entry_price=ep,
                    exit_price=exit_price,
                    shares=shares,
                    pnl=trade_pnl,
                    pnl_pct=(exit_price - ep) / ep,
                    exit_reason=reason,
                    score=score,
                    notes=notes,
                ))
                position = None

        # Entry logic (no position open)
        if position is None:
            prior_close = float(df_day["open"].iloc[0])  # approx prior close for gap calc

            best_signal = None
            for strat in STRATEGIES:
                if strat.name == "gap_bounce" and not _is_morning_window(ts):
                    continue
                sig = strat.evaluate(ticker, window, prior_close=prior_close)
                if sig.triggered:
                    if best_signal is None or sig.score > best_signal.score:
                        best_signal = sig

            if best_signal:
                atr = row.get("atr")
                if pd.isna(atr) or atr <= 0:
                    continue
                stop, target = calc_stop_and_target(price, atr)
                shares = calc_position_size(account_value, price, stop)
                position = (price, stop, target, shares,
                            best_signal.strategy, best_signal.score,
                            best_signal.notes, ts)

    # Force-close any open position at end of day
    if position:
        ep, stop, target, shares, strat, score, notes, entry_ts = position
        last_price = df_day["close"].iloc[-1]
        trade_pnl = (last_price - ep) * shares
        pnl += trade_pnl
        result.trades.append(BacktestTrade(
            ticker=ticker,
            strategy=strat,
            entry_time=str(entry_ts),
            exit_time=str(df_day.index[-1]),
            entry_price=ep,
            exit_price=last_price,
            shares=shares,
            pnl=trade_pnl,
            pnl_pct=(last_price - ep) / ep,
            exit_reason="eod_close",
            score=score,
            notes=notes,
        ))

    return pnl


def run_backtest(
    tickers: List[str],
    start: datetime,
    end: datetime,
    initial_capital: float = config.ACCOUNT_SIZE,
) -> BacktestResult:
    """
    Run full backtest across all tickers and date range.
    Returns a BacktestResult with all trades and metrics.
    """
    result = BacktestResult(
        tickers=tickers,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        initial_capital=initial_capital,
    )
    account_value = initial_capital

    for ticker in tickers:
        logger.info(f"Backtesting {ticker}...")
        try:
            # Fetch daily data chunked into individual days
            df_full = get_bars_yfinance(ticker, start, end, interval="1m")
            if df_full is None or df_full.empty:
                logger.warning(f"No data for {ticker}, skipping")
                continue

            # Group by date and process each trading day separately
            df_full["date"] = df_full.index.date
            for date, df_day in df_full.groupby("date"):
                day_pnl = _run_ticker(ticker, df_day.drop(columns="date"),
                                      result, account_value)
                account_value = max(account_value + day_pnl, 1.0)

        except Exception as e:
            logger.error(f"Error backtesting {ticker}: {e}")

    logger.info(result.summary())
    return result
