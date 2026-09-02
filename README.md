# CashCow Penny Bot

Automated day-trading bot for viral US penny stocks ($0.50–$10) that spike on
news or social momentum. Scans for movers, scores entries with a handful of
structural technical-analysis strategies (support/resistance boxes, W-bottoms,
breakouts, neckline reversals), and manages exits with a trailing stop. Runs
against **Alpaca paper trading** for data + simulated execution; a Webull
broker path exists for the owner's live Canadian account.

> ⚠️ **Not financial advice.** This is a personal research/paper-trading
> project. Nothing here is a recommendation to trade any security. Trading
> penny stocks is high-risk; past backtest results are not indicative of
> future performance.

## Features

- **Scanner** — pulls top movers from Alpaca, filters to common equities
  (excludes leveraged ETFs), enriches with volume + news/catalyst research.
- **Strategies** (`strategies/`) — pluggable, registry-driven (`strategies/registry.py`):
  - `double_bottom` — W-pattern bounce off a double-tested low
  - `trend_reversal` — structural neckline reversal (inverse H&S / rounding
    bottom / cup & handle)
  - `resistance_breakout` — breakout + retest of a 2+ touch resistance level
  - `box_range` — buy tested support, sell tested resistance, inside a
    confirmed trading range
  - `support_bounce` — buy a bounce off any single prior swing low
- **Risk management** (`trading/risk_manager.py`, `trading/portfolio.py`) —
  position sizing off account risk %, per-ticker daily trade/stop caps, exit
  cooldowns, a global minimum stop-distance floor, and commission modeling.
- **Execution** — dedicated exit-monitor thread so trailing stops fire
  independent of scan latency; end-of-day forced flat.
- **Dashboard** (`dashboard/`) — live Dash UI with account stats and a trade
  table.
- **Backtesting** (`backtest_viral.py`, `backtest_1y.py`) — replays the same
  strategy code the live bot uses against historical bars, so live and sim
  can never silently diverge.

## Setup

Requires Python 3.12.

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your own credentials — **never commit this file** (it's
already gitignored). At minimum you need an [Alpaca](https://alpaca.markets)
API key/secret for market data; Webull credentials are only needed if you
intend to route live orders through `BROKER=webull`.

```env
ALPACA_API_KEY=your_alpaca_api_key_here
ALPACA_SECRET_KEY=your_alpaca_secret_key_here
BROKER=alpaca   # or 'webull' for the live Canadian-account path
```

See `.env.example` for the full list of configurable settings (account size,
risk per trade, daily halt %, score threshold, dashboard port, etc.).

## Running

```bash
python main.py                # start the bot
python dashboard/app.py       # dashboard, if not auto-started by main.py
python backtest_viral.py      # backtest against the viral-stock research universe
python backtest_1y.py         # 1-year historical backtest (resumable)
python debug_entries.py       # inspect live scan/signal scoring without trading
```

Dashboard defaults to `http://localhost:8050`.

## Project layout

```
data/         market data, scanner, news/catalyst research
strategies/   entry-signal strategies + the registry that activates them
trading/      broker adapters, portfolio/risk state, position sizing
dashboard/    Dash web UI
backtesting/  backtest engine
tests/        risk-invariant and strategy unit tests
```

## Safety notes

- Only one bot instance may run at a time (a localhost port bind acts as a
  mutex) to avoid two processes trading the same account.
- `config.MIN_STOP_PCT` enforces a minimum stop distance on every trade
  regardless of strategy, applied identically in live and backtest.
- `ENABLED_STRATEGIES` (env var) controls which strategies are live; an
  unknown or empty value raises rather than silently disabling all trading.

## License

Personal project — no license granted for reuse.
