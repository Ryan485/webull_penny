"""
Market data provider.
Primary: Alpaca Data API.
Fallback: yfinance.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

_alpaca_client = None


def _get_alpaca_client():
    global _alpaca_client
    if _alpaca_client is None and config.ALPACA_API_KEY:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            _alpaca_client = StockHistoricalDataClient(
                config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
            )
        except Exception as e:
            logger.warning(f"Alpaca client init failed: {e}")
    return _alpaca_client


def get_bars_alpaca(symbol: str, start: datetime, end: datetime,
                    timeframe: str = "1Min") -> Optional[pd.DataFrame]:
    client = _get_alpaca_client()
    if client is None:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        tf_map = {
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "1Day": TimeFrame(1, TimeFrameUnit.Day),
        }
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf_map.get(timeframe, TimeFrame(1, TimeFrameUnit.Minute)),
            start=start,
            end=end,
            feed="iex",
        )
        bars = client.get_stock_bars(req).df
        if bars.empty:
            return None
        # Alpaca returns multi-index (symbol, timestamp) — drop symbol level
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)
        bars.index = pd.to_datetime(bars.index).tz_convert(ET)
        bars = bars.rename(columns={
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume"
        })
        return bars[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.warning(f"Alpaca bars failed for {symbol}: {e}")
        return None


def get_bars_yfinance(symbol: str, start: datetime, end: datetime,
                      interval: str = "1m") -> Optional[pd.DataFrame]:
    try:
        ticker = yf.Ticker(symbol)
        # yfinance 1m data only available for last 7 days
        df = ticker.history(start=start, end=end, interval=interval, auto_adjust=True)
        if df.empty:
            return None
        df.index = df.index.tz_convert(ET)
        df = df.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume"
        })
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.warning(f"yfinance bars failed for {symbol}: {e}")
        return None


def get_live_bars(symbol: str, lookback_minutes: int = 390) -> Optional[pd.DataFrame]:
    """
    Fetch recent 1-minute bars for live trading.
    Tries Alpaca first, falls back to yfinance.
    """
    now = datetime.now(ET)
    start = now - timedelta(minutes=lookback_minutes + 30)

    df = get_bars_alpaca(symbol, start, now, timeframe="1Min")
    if df is None or df.empty:
        df = get_bars_yfinance(symbol, start, now, interval="1m")
    if df is None or len(df) < 50:
        logger.warning(f"Not enough data for {symbol}")
        return None

    # Reject stale data — last bar must be within 10 minutes during market hours
    if is_market_open():
        last_bar = df.index[-1]
        if hasattr(last_bar, "tzinfo") and last_bar.tzinfo is None:
            last_bar = last_bar.tz_localize(ET)
        age_mins = (now - last_bar).total_seconds() / 60
        if age_mins > 10:
            logger.warning(f"Stale data for {symbol}: last bar is {age_mins:.0f}min old")
            return None

    return df.tail(lookback_minutes)


def get_chart_bars(symbol: str) -> tuple[Optional[pd.DataFrame], str]:
    """
    Fetch today's 1-minute bars for charting only.
    Returns (df, source_label). Tries Alpaca → yfinance.
    """
    now = datetime.now(ET)
    today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    start = today_open if now > today_open else now - timedelta(hours=24)

    df = get_bars_alpaca(symbol, start, now, timeframe="1Min")
    if df is not None and not df.empty:
        return df, "Alpaca"

    try:
        ticker = yf.Ticker(symbol)
        df_yf = ticker.history(period="1d", interval="1m", auto_adjust=True)
        if not df_yf.empty:
            df_yf.index = df_yf.index.tz_convert(ET)
            df_yf = df_yf.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"
            })
            return df_yf[["open", "high", "low", "close", "volume"]], "yFinance"
    except Exception as e:
        logger.warning(f"yfinance chart fallback failed for {symbol}: {e}")

    return None, ""


def get_historical_bars(symbol: str, start: datetime, end: datetime,
                        interval: str = "1m") -> Optional[pd.DataFrame]:
    """Fetch historical bars for backtesting (yfinance preferred for longer history)."""
    yf_interval = interval
    df = get_bars_yfinance(symbol, start, end, interval=yf_interval)
    if df is None or df.empty:
        df = get_bars_alpaca(symbol, start, end,
                             timeframe=interval.replace("m", "Min"))
    return df


def get_prior_close(symbol: str) -> Optional[float]:
    """Return the previous trading day's closing price."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d", auto_adjust=True)
        if len(hist) >= 2:
            return float(hist["Close"].iloc[-2])
    except Exception as e:
        logger.warning(f"Prior close failed for {symbol}: {e}")
    return None


def is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now < close_t
