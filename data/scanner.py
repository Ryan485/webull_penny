"""
Viral stock scanner — powered by Alpaca's real-time movers endpoint.
Filters to $1–$10, high absolute volume, and 3x+ relative volume (the viral signal).
Saves daily results to logs/ for building a backtesting database over time.
"""
import csv
import json
import logging
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List

import pytz

import config

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

# {symbol: (rel_vol, date_str)} — reused across scan cycles, reset each new day
_rel_vol_cache: dict = {}

# {symbol: quoteType} — permanent per-process cache; an asset's type never changes
_quote_type_cache: dict = {}


def _is_common_stock(symbol: str) -> bool:
    """
    True only for real common stocks. Filters out leveraged/inverse ETFs
    (SOXS, TQQQ, ...) that land on the movers list on big market days —
    they trade like pennies price-wise but are NOT viral penny stocks
    (added 2026-07-13 after SOXS kept showing up on the watchlist).
    Uses Yahoo's quoteType (EQUITY vs ETF/MUTUALFUND/INDEX). Fails OPEN
    (returns True) on network errors so a Yahoo outage can't empty the
    watchlist; the failed symbol is retried on the next scan cycle.
    """
    cached = _quote_type_cache.get(symbol)
    if cached is not None:
        return cached == "EQUITY"
    url = (f"https://query1.finance.yahoo.com/v1/finance/search"
           f"?q={symbol}&quotesCount=5&newsCount=0")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        qtype = None
        for q in data.get("quotes", []):
            if q.get("symbol") == symbol:
                qtype = q.get("quoteType", "")
                break
        if qtype is None:
            return True  # unknown to Yahoo — don't block on missing data
        _quote_type_cache[symbol] = qtype
        if qtype != "EQUITY":
            logger.info(f"Scanner: skipping {symbol} (quoteType={qtype}, not a stock)")
        return qtype == "EQUITY"
    except Exception as e:
        logger.warning(f"quoteType lookup failed for {symbol}: {e}")
        return True


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "Accept": "application/json",
    }


def _fetch_volumes(symbols: List[str]) -> dict:
    """One batch snapshots call → {symbol: daily_volume}. IEX feed."""
    if not symbols:
        return {}
    syms = ",".join(symbols)
    url = f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed=iex"
    try:
        req = urllib.request.Request(url, headers=_alpaca_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        out = {}
        for sym, snap in data.items():
            db = (snap or {}).get("dailyBar") or {}
            out[sym] = int(db.get("v", 0))
        return out
    except Exception as e:
        logger.warning(f"Snapshots fetch failed: {e}")
        return {}


def _fetch_alpaca_movers() -> List[dict]:
    """
    Call Alpaca's real-time movers endpoint + snapshots for volume.
    Returns {symbol, price, change_pct, volume, rel_vol} dicts.
    """
    url = "https://data.alpaca.markets/v1beta1/screener/stocks/movers?top=50"
    req = urllib.request.Request(url, headers=_alpaca_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    raw = []
    for item in data.get("gainers", []):
        try:
            raw.append({
                "symbol":     item["symbol"],
                "price":      float(item.get("price", 0)),
                "change_pct": float(item.get("percent_change", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue

    volumes = _fetch_volumes([r["symbol"] for r in raw])
    for r in raw:
        r["volume"] = volumes.get(r["symbol"], 0)
        r["rel_vol"] = 0.0
    return raw


def _fetch_rel_volumes(symbols: List[str]) -> dict:
    """
    Returns {symbol: rel_vol} using a per-day cache.
    Only fetches symbols not already cached today — subsequent scan cycles return instantly.
    """
    if not symbols:
        return {}

    today = datetime.now(ET).strftime("%Y-%m-%d")
    result = {}
    to_fetch = []
    for sym in symbols:
        cached = _rel_vol_cache.get(sym)
        if cached and cached[1] == today:
            result[sym] = cached[0]
        else:
            to_fetch.append(sym)

    if not to_fetch:
        return result

    try:
        import yfinance as yf
        raw = yf.download(
            to_fetch, period="22d", interval="1d",
            auto_adjust=True, progress=False,
        )
        if not raw.empty:
            vol_df = raw[["Volume"]].rename(columns={"Volume": to_fetch[0]}) \
                if len(to_fetch) == 1 else raw["Volume"]
            for sym in to_fetch:
                try:
                    series = vol_df[sym].dropna()
                    if len(series) < 5:
                        continue
                    avg = series.iloc[:-1].tail(20).mean()
                    today_vol = float(series.iloc[-1])
                    if avg > 0:
                        rv = today_vol / avg
                        _rel_vol_cache[sym] = (rv, today)
                        result[sym] = rv
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Rel-vol fetch failed: {e}")

    return result


def scan_movers() -> List[str]:
    """
    Pull Alpaca's live top-gainers and filter for viral penny stocks:
    - Price $1–$10
    - Volume ≥ SCAN_MIN_VOLUME (300K)
    - Sorted by volume descending, then % change descending.
    Alpaca movers are already pre-filtered for significant movement,
    so no secondary yfinance rel-vol check is needed.
    """
    try:
        movers = _fetch_alpaca_movers()

        viral = [
            p for p in movers
            if config.SCAN_PRICE_MIN <= p["price"] <= config.SCAN_PRICE_MAX
            and p["volume"] >= config.SCAN_MIN_VOLUME
            and _is_common_stock(p["symbol"])  # no leveraged ETFs (SOXS etc.)
        ]

        seen = set()
        tickers = []
        for p in sorted(viral, key=lambda x: (x["volume"], x["change_pct"]), reverse=True):
            if p["symbol"] not in seen:
                seen.add(p["symbol"])
                tickers.append(p["symbol"])
                logger.debug(
                    f"  {p['symbol']:8s} ${p['price']:.2f} "
                    f"{p['change_pct']:+.1f}% vol={p['volume']:,}"
                )

        logger.info(f"Alpaca scan: {len(tickers)} viral penny stocks found")
        if tickers:
            _save_scan_results(tickers)

        _research_new(viral, seen)
        return tickers

    except Exception as e:
        logger.warning(f"Alpaca movers scan failed: {e}")
        return []


def _research_new(viral: list, seen: set) -> None:
    """Dispatch background research for each ticker that made the final list."""
    try:
        from data.research import research_viral_stock
    except ImportError:
        return
    candidates = [p for p in viral if p["symbol"] in seen]
    if not candidates:
        return
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="research")
    for p in candidates:
        executor.submit(
            research_viral_stock,
            p["symbol"], p["price"], p["volume"], p["rel_vol"], p["change_pct"],
        )
    executor.shutdown(wait=False)


def get_watchlist() -> List[str]:
    """Return live viral stocks from Alpaca scan only."""
    return scan_movers()


def _save_scan_results(tickers: List[str]) -> None:
    os.makedirs("logs", exist_ok=True)
    path = "logs/scan_history.csv"
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    existing: set = set()
    if os.path.exists(path):
        with open(path, "r") as f:
            for row in csv.DictReader(f):
                if row.get("date") == date_str:
                    existing.add(row.get("ticker"))
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if os.path.getsize(path) == 0:
            writer.writerow(["date", "ticker"])
        for t in tickers:
            if t not in existing:
                writer.writerow([date_str, t])


def load_historical_viral() -> dict:
    """Load saved scan history for backtesting. Returns {date_str: [ticker, ...]}"""
    path = "logs/scan_history.csv"
    result: dict = {}
    if not os.path.exists(path):
        return result
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            d = row.get("date", "")
            t = row.get("ticker", "")
            if d and t:
                result.setdefault(d, []).append(t)
    return result
