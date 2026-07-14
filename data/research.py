"""
Viral stock research module.
When a stock is detected as viral, this module fetches:
  - Recent news headlines (yfinance)
  - StockTwits post count and sentiment (public API, no auth)
  - Infers a catalyst category from headlines + social data

Each research result is saved to logs/viral_research/YYYY-MM-DD_TICKER.json
A summary row is also appended to logs/viral_research_summary.csv for pattern analysis.
"""
import csv
import json
import logging
import os
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

RESEARCH_DIR = "logs/viral_research"
SUMMARY_CSV = "logs/viral_research_summary.csv"

SUMMARY_FIELDS = [
    "date", "ticker", "detected_at", "price", "volume",
    "rel_vol", "change_pct", "catalyst",
    "news_count", "st_posts", "st_bullish", "st_bearish",
    "top_headline",
]

# Catalyst keyword map — checked in order, first match wins.
# Keywords must be specific enough to avoid false positives on generic words
# (e.g. bare "phase" matched "next phase of its blockchain strategy").
_CATALYST_RULES = [
    ("fda_catalyst",        ["fda", "clinical trial", "phase 1", "phase 2", "phase 3",
                             "phase i", "phase ii", "phase iii", "drug approval",
                             "nda", "bla", "biotech", "therapeutic"]),
    ("crypto_blockchain",   ["blockchain", "crypto", "bitcoin", "ethereum", "token",
                             "chainlink", "web3", "defi", "stablecoin"]),
    ("merger_acquisition",  ["merger", "acquisition", "buyout", "takeover", "acquir"]),
    ("earnings",            ["earnings", "revenue", "guidance", "eps beat",
                             "quarterly results", "q1 results", "q2 results",
                             "q3 results", "q4 results"]),
    ("partnership_deal",    ["partnership", "collaboration", "contract award",
                             "license agreement", "strategic agreement"]),
    ("share_offering",      ["offering", "dilut", "prospectus", "at-the-market",
                             "reverse split"]),
    ("short_squeeze",       ["short squeeze", "short interest", "reddit", "wsb",
                             "wallstreetbets", "meme stock"]),
    ("sector_momentum",     ["sector rally", "industry momentum", " ai ",
                             "artificial intelligence", "clean energy", "quantum"]),
]


ARTICLE_TEXT_MAX = 3000      # chars of scraped article body to keep
ARTICLE_SCRAPE_TOP = 5       # scrape full text for this many newest articles


def _fetch_news_yfinance(ticker: str) -> list:
    """Handles both the new (nested `content`) and legacy flat yfinance schemas."""
    try:
        import yfinance as yf
        raw = yf.Ticker(ticker).news or []
        out = []
        for n in raw[:15]:
            c = n.get("content") or n  # new schema nests under "content"
            title = c.get("title", "")
            if not title:
                continue
            provider = c.get("provider") or {}
            url = (c.get("canonicalUrl") or {}).get("url") \
                or (c.get("clickThroughUrl") or {}).get("url") \
                or n.get("link", "")
            out.append({
                "title": title,
                "summary": c.get("summary", ""),
                "publisher": provider.get("displayName") or n.get("publisher", ""),
                "published": c.get("pubDate") or c.get("displayTime", ""),
                "link": url,
            })
        return out
    except Exception as e:
        logger.warning(f"News fetch failed {ticker}: {e}")
        return []


def _fetch_news_yahoo_search(ticker: str) -> list:
    """
    Yahoo Finance search API — the same news list shown on the quote page
    (finance.yahoo.com/quote/TICKER). Catches articles the yfinance .news
    call misses (VTAK 2026-07-08: quote page had a dozen, .news returned 1).
    """
    try:
        url = ("https://query1.finance.yahoo.com/v1/finance/search"
               f"?q={ticker}&newsCount=15&quotesCount=0")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        out = []
        for n in data.get("news", []):
            title = n.get("title", "")
            if not title:
                continue
            ts = n.get("providerPublishTime")
            published = (
                datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
                if isinstance(ts, (int, float)) else ""
            )
            out.append({
                "title": title,
                "summary": "",
                "publisher": n.get("publisher", ""),
                "published": published,
                "link": n.get("link", ""),
            })
        return out
    except Exception as e:
        logger.warning(f"Yahoo search news failed {ticker}: {e}")
        return []


class _ParagraphExtractor(HTMLParser):
    """Collect visible <p> text, skipping script/style."""
    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_p = False
        self.paragraphs = []
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "p" and not self._skip:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        elif tag == "p" and self._in_p:
            text = "".join(self._buf).strip()
            if len(text) > 40:  # drop nav/boilerplate fragments
                self.paragraphs.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_p and not self._skip:
            self._buf.append(data)


def _fetch_article_text(url: str) -> str:
    """Best-effort scrape of an article body. Empty string on any failure."""
    if not url or not url.startswith("http"):
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(500_000).decode("utf-8", errors="replace")
        parser = _ParagraphExtractor()
        parser.feed(html)
        return " ".join(parser.paragraphs)[:ARTICLE_TEXT_MAX]
    except Exception:
        return ""


def _fetch_news(ticker: str) -> list:
    """Merged news from yfinance + Yahoo search, newest first, with full
    article text scraped for the top ARTICLE_SCRAPE_TOP items."""
    merged = []
    seen = set()
    for item in _fetch_news_yfinance(ticker) + _fetch_news_yahoo_search(ticker):
        key = item["title"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda n: n.get("published") or "", reverse=True)
    merged = merged[:15]
    for item in merged[:ARTICLE_SCRAPE_TOP]:
        text = _fetch_article_text(item["link"])
        if text:
            item["text"] = text
    return merged


def _fetch_stocktwits(ticker: str) -> dict:
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        messages = data.get("messages", [])
        # entities.sentiment can be null — guard every level
        sentiments = [
            ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            for m in messages
        ]
        bullish = sentiments.count("Bullish")
        bearish = sentiments.count("Bearish")
        # Sample of recent message bodies for later pattern analysis
        sample = [
            {"body": (m.get("body") or "")[:200], "created_at": m.get("created_at", "")}
            for m in messages[:5]
        ]
        return {"post_count": len(messages), "bullish": bullish,
                "bearish": bearish, "sample_posts": sample}
    except Exception as e:
        logger.warning(f"StockTwits fetch failed {ticker}: {e}")
        return {"post_count": 0, "bullish": 0, "bearish": 0, "sample_posts": []}


def _infer_catalyst(news: list, stocktwits: dict) -> str:
    # Scraped article text (first 600 chars each) sharpens classification —
    # headlines alone are often too vague to match a catalyst keyword.
    text = " ".join(
        f"{n.get('title', '')} {n.get('summary', '')} "
        f"{(n.get('text') or '')[:600]}".lower()
        for n in news
    )
    for category, keywords in _CATALYST_RULES:
        if any(kw in text for kw in keywords):
            return category
    if not news and stocktwits.get("post_count", 0) >= 5:
        return "social_media_driven"
    if news:
        return "news_driven_unknown"
    return "unknown"


def _compute_rel_vol(ticker: str) -> float:
    """Today's volume vs 20-day average. Slow (yfinance) — background thread only."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="22d", interval="1d", auto_adjust=True)
        vol = hist["Volume"].dropna()
        if len(vol) < 5:
            return 0.0
        avg = vol.iloc[:-1].tail(20).mean()
        if avg > 0:
            return float(vol.iloc[-1] / avg)
    except Exception as e:
        logger.debug(f"Rel-vol failed {ticker}: {e}")
    return 0.0


def _append_summary(record: dict) -> None:
    os.makedirs(os.path.dirname(SUMMARY_CSV), exist_ok=True)
    write_header = not os.path.exists(SUMMARY_CSV) or os.path.getsize(SUMMARY_CSV) == 0
    top_headline = record["news"][0]["title"] if record["news"] else ""
    row = {
        "date":         record["date"],
        "ticker":       record["ticker"],
        "detected_at":  record["detected_at"],
        "price":        record["price"],
        "volume":       record["volume"],
        "rel_vol":      record["rel_vol"],
        "change_pct":   record["change_pct"],
        "catalyst":     record["catalyst"],
        "news_count":   len(record["news"]),
        "st_posts":     record["stocktwits"]["post_count"],
        "st_bullish":   record["stocktwits"]["bullish"],
        "st_bearish":   record["stocktwits"]["bearish"],
        "top_headline": top_headline,
    }
    with open(SUMMARY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def get_catalyst(ticker: str) -> Optional[str]:
    """
    Return today's researched catalyst for a ticker, or None if not researched yet.
    Cheap file read — safe to call from the trading loop.
    """
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    path = os.path.join(RESEARCH_DIR, f"{date_str}_{ticker}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("catalyst")
    except Exception:
        return None


def research_viral_stock(
    ticker: str,
    price: float,
    volume: int,
    rel_vol: float,
    change_pct: float,
) -> Optional[dict]:
    """
    Research why a stock went viral. Idempotent — skips if already done today.
    Saves full JSON to logs/viral_research/YYYY-MM-DD_TICKER.json
    and appends a summary row to logs/viral_research_summary.csv.
    Returns the record dict, or None if skipped.
    """
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    date_str = datetime.now(ET).strftime("%Y-%m-%d")
    out_path = os.path.join(RESEARCH_DIR, f"{date_str}_{ticker}.json")

    if os.path.exists(out_path):
        return None  # already researched today

    logger.info(f"Researching viral stock: {ticker}")
    news = _fetch_news(ticker)
    stocktwits = _fetch_stocktwits(ticker)
    catalyst = _infer_catalyst(news, stocktwits)
    if not rel_vol:
        rel_vol = _compute_rel_vol(ticker)

    record = {
        "ticker":      ticker,
        "date":        date_str,
        "detected_at": datetime.now(ET).isoformat(),
        "price":       price,
        "volume":      volume,
        "rel_vol":     round(rel_vol, 2),
        "change_pct":  round(change_pct, 2),
        "catalyst":    catalyst,
        "news":        news,
        "stocktwits":  stocktwits,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)

    _append_summary(record)

    logger.info(
        f"  {ticker}: catalyst={catalyst}  relVol={rel_vol:.1f}x  "
        f"news={len(news)}  ST={stocktwits['post_count']} posts "
        f"({stocktwits['bullish']}B/{stocktwits['bearish']}Be)"
    )
    return record
