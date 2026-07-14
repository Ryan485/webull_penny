"""
All technical indicators used by the four strategies.
Uses pandas-ta (pure Python, no compilation required).
"""
import pandas as pd
import numpy as np
import pandas_ta as ta
import config


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Expects OHLCV DataFrame with columns: open, high, low, close, volume.
    Returns the same DataFrame with indicator columns appended.
    """
    df = df.copy()

    # RSI
    df["rsi"] = ta.rsi(df["close"], length=config.RSI_PERIOD)

    # MACD
    macd = ta.macd(df["close"],
                   fast=config.MACD_FAST,
                   slow=config.MACD_SLOW,
                   signal=config.MACD_SIGNAL)
    df["macd"] = macd[f"MACD_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]
    df["macd_signal"] = macd[f"MACDs_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]
    df["macd_hist"] = macd[f"MACDh_{config.MACD_FAST}_{config.MACD_SLOW}_{config.MACD_SIGNAL}"]

    # Stochastic fast (5,3,3)
    k, d, sk = config.STOCH_FAST
    stf = ta.stoch(df["high"], df["low"], df["close"], k=k, d=d, smooth_k=sk)
    df["stoch_k_fast"] = stf[f"STOCHk_{k}_{d}_{sk}"]
    df["stoch_d_fast"] = stf[f"STOCHd_{k}_{d}_{sk}"]

    # Stochastic medium (10,5,5)
    k, d, sk = config.STOCH_MED
    stm = ta.stoch(df["high"], df["low"], df["close"], k=k, d=d, smooth_k=sk)
    df["stoch_k_med"] = stm[f"STOCHk_{k}_{d}_{sk}"]
    df["stoch_d_med"] = stm[f"STOCHd_{k}_{d}_{sk}"]

    # VWAP (resets daily — works correctly when df is intraday)
    df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])

    # ATR
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=config.ATR_PERIOD)

    # Moving averages
    df["ma5"] = ta.sma(df["close"], length=config.MA_SHORT)
    df["ma10"] = ta.sma(df["close"], length=config.MA_MID)

    # Rolling average volume
    df["avg_vol"] = df["volume"].rolling(config.AVG_VOL_PERIOD).mean()

    return df


def latest(df: pd.DataFrame) -> pd.Series:
    """Return the last fully-formed candle row."""
    return df.iloc[-1]


def prev(df: pd.DataFrame, n: int = 1) -> pd.Series:
    """Return n candles back from the last."""
    return df.iloc[-(n + 1)]
