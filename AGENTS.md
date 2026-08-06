# AGENTS.md - CashCow Penny Bot

## Code Review Protocol

For every code review, you must read and follow `CODEX_REVIEW.md` in full.

The instructions in `CODEX_REVIEW.md` are mandatory. Act only as an independent,
read-only reviewer. Never modify, stage, commit, or push files during review.

## Project specifics for reviewers

* Automated day trader for viral US penny stocks. Currently paper trading on
  Alpaca; IBKR Canada at go-live. Real capital is the intended destination.
* Runs on `py -3.12` (never plain `python`).
* Windows console is cp949: ASCII only in anything logged. Non-ASCII in a log
  message crashes console logging and is a valid finding.
* Strategy parameters are frozen and versioned via `config.STRATEGY_VERSION`.
  A change to signal logic or parameters requires a version bump so
  forward-test samples do not mix. A missing bump is a valid finding.
* Stop-distance logic is intentionally mirrored in `main.py` and
  `backtest_viral.py` (`config.MIN_STOP_PCT`). These must not drift; a
  hardcoded literal in either file is a valid finding.
* IEX is the live data feed. Its volume is ~2-3% of consolidated volume and its
  ATR understates volatility on thin names. Do not report the resulting
  magnitudes as bugs; they are known and documented in `CLAUDE.md`.
* Position size is `min(2%-risk shares, 20%-of-account cost cap shares)`. The
  cost cap has bound on every live trade to date, so a wider stop does NOT
  reduce share count on this bot. Reject any reasoning that assumes it does.
* Never propose weakening, removing, or bypassing a risk limit to make a test
  pass.
* Deeper architecture and the full decision history live in `CLAUDE.md`.

## Secrets

`.env` (Alpaca API keys) and `did.bin` are gitignored and must never be read,
printed, quoted, or committed. If you can see them, do not open them; report the
exposure instead.
