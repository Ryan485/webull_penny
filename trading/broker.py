"""
Broker abstraction layer.

BROKER options in .env:
  webull_paper  — Webull virtual trading account (paper trades on Webull)  ← use this
  webull        — Webull live trading (real money, needs trading PIN)
  paper         — local simulation only, no broker connection
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional

import config

logger = logging.getLogger(__name__)


class BaseBroker(ABC):
    @abstractmethod
    def get_account_value(self) -> float: ...

    @abstractmethod
    def place_market_buy(self, ticker: str, shares: int) -> bool: ...

    @abstractmethod
    def place_market_sell(self, ticker: str, shares: int) -> bool: ...

    @abstractmethod
    def get_quote(self, ticker: str) -> Optional[float]: ...


# ── Shared Webull login ───────────────────────────────────────────────────────

def _webull_login(wb) -> None:
    """Authenticate a webull or paper_webull instance using captured token."""
    if config.WEBULL_DEVICE_ID:
        wb._set_did(config.WEBULL_DEVICE_ID)

    if config.WEBULL_ACCESS_TOKEN:
        wb.api_login(
            access_token=config.WEBULL_ACCESS_TOKEN,
            refresh_token=config.WEBULL_REFRESH_TOKEN,
            token_expire=config.WEBULL_TOKEN_EXPIRE,
            uuid=config.WEBULL_UUID,
        )
    elif config.WEBULL_EMAIL and config.WEBULL_PASSWORD:
        result = wb.login(config.WEBULL_EMAIL, config.WEBULL_PASSWORD)
        if "accessToken" not in str(result):
            raise RuntimeError("Webull login failed. Run capture_webull_token.py.")
    else:
        raise RuntimeError("No Webull credentials. Run capture_webull_token.py.")


# ── Webull Paper Trading ──────────────────────────────────────────────────────

class WebullPaperBroker(BaseBroker):
    """
    Webull virtual trading — orders go to your Webull paper account.
    Uses paper_webull from the webull library.
    No trading PIN needed.
    """

    def __init__(self):
        try:
            from webull import paper_webull
            self._wb = paper_webull()
            _webull_login(self._wb)
            logger.info("Webull paper trading ready")
        except ImportError:
            raise RuntimeError("webull package not installed: pip install webull")

    def get_account_value(self) -> float:
        try:
            acct = self._wb.get_account()
            return float(acct.get("netLiquidation", config.ACCOUNT_SIZE))
        except Exception as e:
            logger.warning(f"Webull paper account error: {e}")
            return config.ACCOUNT_SIZE

    def place_market_buy(self, ticker: str, shares: int) -> bool:
        try:
            result = self._wb.place_order(
                stock=ticker,
                action="BUY",
                orderType="MKT",
                enforce="DAY",
                quant=shares,
            )
            logger.info(f"[WEBULL PAPER] BUY {ticker} x{shares}: {result}")
            return True
        except Exception as e:
            logger.error(f"Webull paper BUY failed {ticker}: {e}")
            return False

    def place_market_sell(self, ticker: str, shares: int) -> bool:
        try:
            result = self._wb.place_order(
                stock=ticker,
                action="SELL",
                orderType="MKT",
                enforce="DAY",
                quant=shares,
            )
            logger.info(f"[WEBULL PAPER] SELL {ticker} x{shares}: {result}")
            return True
        except Exception as e:
            logger.error(f"Webull paper SELL failed {ticker}: {e}")
            return False

    def get_quote(self, ticker: str) -> Optional[float]:
        try:
            q = self._wb.get_quote(ticker)
            return float(q.get("close") or q.get("pPrice") or 0) or None
        except Exception as e:
            logger.warning(f"Webull quote error {ticker}: {e}")
            return None

    def get_open_positions(self) -> list:
        """Return list of dicts with ticker, shares, cost_price from Webull paper account."""
        try:
            raw = self._wb.get_positions()
            if not isinstance(raw, list):
                return []
            result = []
            for p in raw:
                sym = p.get("ticker", {}).get("symbol", "")
                shares = int(float(p.get("position", 0)))
                cost = float(p.get("costPrice", 0))
                last = float(p.get("lastPrice", cost))
                if sym and shares > 0:
                    result.append({
                        "ticker": sym, "shares": shares,
                        "cost_price": cost, "last_price": last,
                    })
            return result
        except Exception as e:
            logger.warning(f"get_open_positions error: {e}")
            return []


# ── Webull Live Trading ───────────────────────────────────────────────────────

class WebullBroker(BaseBroker):
    """Real money trading on your Canadian Webull account. Needs trading PIN."""

    def __init__(self):
        try:
            from webull import webull
            self._wb = webull()
            _webull_login(self._wb)
            if config.WEBULL_TRADING_PIN:
                self._wb.get_trade_token(config.WEBULL_TRADING_PIN)
            else:
                raise RuntimeError("WEBULL_TRADING_PIN required for live trading.")
            logger.info("Webull live trading ready")
        except ImportError:
            raise RuntimeError("webull package not installed: pip install webull")

    def get_account_value(self) -> float:
        try:
            acct = self._wb.get_account()
            return float(acct.get("netLiquidation", config.ACCOUNT_SIZE))
        except Exception as e:
            logger.warning(f"Webull account error: {e}")
            return config.ACCOUNT_SIZE

    def place_market_buy(self, ticker: str, shares: int) -> bool:
        try:
            result = self._wb.place_order(
                stock=ticker, action="BUY", orderType="MKT",
                enforce="DAY", quant=shares,
            )
            logger.info(f"[WEBULL LIVE] BUY {ticker} x{shares}: {result}")
            return True
        except Exception as e:
            logger.error(f"Webull LIVE BUY failed {ticker}: {e}")
            return False

    def place_market_sell(self, ticker: str, shares: int) -> bool:
        try:
            result = self._wb.place_order(
                stock=ticker, action="SELL", orderType="MKT",
                enforce="DAY", quant=shares,
            )
            logger.info(f"[WEBULL LIVE] SELL {ticker} x{shares}: {result}")
            return True
        except Exception as e:
            logger.error(f"Webull LIVE SELL failed {ticker}: {e}")
            return False

    def get_quote(self, ticker: str) -> Optional[float]:
        try:
            q = self._wb.get_quote(ticker)
            return float(q.get("close") or q.get("pPrice") or 0) or None
        except Exception as e:
            logger.warning(f"Webull quote error {ticker}: {e}")
            return None


# ── Local simulation ──────────────────────────────────────────────────────────

class PaperBroker(BaseBroker):
    """Fully local simulation — no broker connection at all."""

    def __init__(self):
        logger.info("Local paper broker — no orders sent anywhere")

    def get_account_value(self) -> float:
        return config.ACCOUNT_SIZE

    def place_market_buy(self, ticker: str, shares: int) -> bool:
        logger.info(f"[LOCAL SIM] BUY {ticker} x{shares}")
        return True

    def place_market_sell(self, ticker: str, shares: int) -> bool:
        logger.info(f"[LOCAL SIM] SELL {ticker} x{shares}")
        return True

    def get_quote(self, ticker: str) -> Optional[float]:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="1d", interval="1m")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return None


# ── Alpaca Paper Trading ──────────────────────────────────────────────────────

class AlpacaBroker(BaseBroker):
    """
    Alpaca paper trading — orders visible at paper-api.alpaca.markets.
    Uses your existing Alpaca API keys. No extra setup needed.
    """

    def __init__(self):
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
            self._client = TradingClient(
                config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True
            )
            self._data = StockHistoricalDataClient(
                config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
            )
            acct = self._client.get_account()
            logger.info(f"Alpaca paper trading ready | equity=${float(acct.equity):,.2f}")
        except ImportError:
            raise RuntimeError("alpaca-py not installed: pip install alpaca-py")

    def get_account_value(self) -> float:
        try:
            return float(self._client.get_account().equity)
        except Exception as e:
            logger.warning(f"Alpaca account error: {e}")
            return config.ACCOUNT_SIZE

    def _wait_for_fill(self, order_id, timeout: float = 6.0) -> Optional[float]:
        """Poll until the order is filled and return filled_avg_price, or None on timeout."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                o = self._client.get_order_by_id(str(order_id))
                if o.status.value in ("filled", "partially_filled") and o.filled_avg_price:
                    return float(o.filled_avg_price)
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def place_market_buy(self, ticker: str, shares: int):
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            fill_price = self._wait_for_fill(order.id)
            logger.info(
                f"[ALPACA PAPER] BUY {ticker} x{shares}: "
                f"id={order.id} fill=${fill_price:.4f}" if fill_price else
                f"[ALPACA PAPER] BUY {ticker} x{shares}: id={order.id} (fill pending)"
            )
            return fill_price or True
        except Exception as e:
            logger.error(f"Alpaca BUY failed {ticker}: {e}")
            return None

    def place_market_sell(self, ticker: str, shares: int):
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            req = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            fill_price = self._wait_for_fill(order.id)
            logger.info(
                f"[ALPACA PAPER] SELL {ticker} x{shares}: "
                f"id={order.id} fill=${fill_price:.4f}" if fill_price else
                f"[ALPACA PAPER] SELL {ticker} x{shares}: id={order.id} (fill pending)"
            )
            return fill_price or True
        except Exception as e:
            err = str(e)
            if "42210000" in err or "cannot be sold short" in err:
                logger.warning(
                    f"Alpaca SELL {ticker}: no broker position (phantom entry) — forcing portfolio cleanup"
                )
                return True
            logger.error(f"Alpaca SELL failed {ticker}: {e}")
            return None

    def get_quote(self, ticker: str) -> Optional[float]:
        # Latest TRADE first, not the quote midpoint: on thin sub-$1 names
        # the IEX book is often one-sided, so the midpoint collapses far
        # below the market and instantly trips stops (BIYA 2026-07-08:
        # bought $0.5085, "stop" at $0.48 fired 11s later while actual
        # prints were $0.505+).
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            req = StockLatestTradeRequest(symbol_or_symbols=ticker, feed="iex")
            t = self._data.get_stock_latest_trade(req)
            trade = t.get(ticker)
            if trade and trade.price and trade.price > 0:
                return float(trade.price)
        except Exception:
            pass
        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed="iex")
            q = self._data.get_stock_latest_quote(req)
            quote = q.get(ticker)
            if quote and quote.bid_price > 0 and quote.ask_price > quote.bid_price:
                mid = float((quote.ask_price + quote.bid_price) / 2)
                spread_pct = float(quote.ask_price - quote.bid_price) / mid
                if spread_pct <= 0.05:  # only trust a sane two-sided book
                    return mid
        except Exception:
            pass
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="1d", interval="1m")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception:
            pass
        return None

    def get_open_positions(self) -> list:
        try:
            positions = self._client.get_all_positions()
            return [
                {
                    "ticker": p.symbol,
                    "shares": int(float(p.qty)),
                    "cost_price": float(p.avg_entry_price),
                    "last_price": float(p.current_price or p.avg_entry_price),
                }
                for p in positions
                if float(p.qty) > 0
            ]
        except Exception as e:
            logger.warning(f"Alpaca get_positions error: {e}")
            return []


# ── Factory ───────────────────────────────────────────────────────────────────

def create_broker() -> BaseBroker:
    name = config.BROKER.lower()
    if name == "alpaca":
        logger.info("Broker: Alpaca paper trading")
        return AlpacaBroker()
    elif name == "webull_paper":
        logger.info("Broker: Webull paper trading")
        return WebullPaperBroker()
    elif name == "webull":
        logger.info("Broker: Webull live trading")
        return WebullBroker()
    else:
        logger.info("Broker: local simulation")
        return PaperBroker()
