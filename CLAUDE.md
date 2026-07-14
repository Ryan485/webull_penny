# CashCow Penny Bot

> **Maintenance rule:** if you change strategy logic, entry/exit gates, config
> defaults, broker setup, or learn something non-obvious the hard way, update
> this file in the same session — it is the shared context for all future
> sessions (and the crypto bot sessions at C:\cashcow\crypto_kraken).

Automated day trader for viral US penny stocks ($0.50–$10) that spiked on news/social
momentum. Owner is in Canada; **paper trading on Alpaca** (BROKER=alpaca in .env).
Sister project: crypto bot at `C:\cashcow\crypto_kraken` (same architecture, Kraken).

## Run
- Bot: `py -3.12 main.py` (always py -3.12, not `python`) — restart with Ctrl+C then rerun
- Dashboard: http://localhost:8050 (8060 belongs to the crypto bot). Redesigned
  2026-07-14 (owner's Korean-bot reference): stat cards + flat BUY/SELL trades
  table, 5s refresh; the candlestick chart was REMOVED on request — don't
  re-add it. After a bot restart, hard-refresh the browser tab (Ctrl+Shift+R)
  or stale cached callbacks throw KeyErrors.
- Debug scan/signals: `py -3.12 debug_entries.py`
- Backtests: `py -3.12 backtest_viral.py` (viral-research universe),
  `py -3.12 backtest_1y.py` (1-year, resumable via logs/reports checkpoint files)

## Architecture
- `data/scanner.py` — Alpaca movers endpoint → watchlist. **Movers returns NO volume field**;
  volumes come from a separate batch snapshots call (`_fetch_volumes`). Don't remove it —
  without it every ticker has volume=0 and the watchlist is empty. Non-stocks
  (leveraged ETFs like SOXS/TQQQ that land on movers on big market days) are
  filtered via Yahoo search quoteType == EQUITY (`_is_common_stock`, cached,
  fails open on network errors) — added 2026-07-13, owner wants stocks only.
- `data/market_data.py` — 1m bars, Alpaca IEX feed with yfinance fallback; stale-data guard.
- `data/research.py` — viral-stock research: news merged from yfinance AND the
  Yahoo Finance search API (`query1.finance.yahoo.com/v1/finance/search` — same
  list as the quote page; yfinance .news alone returned 1 stale VTAK article
  when the page had a dozen, fixed 2026-07-08), newest-first, full article text
  scraped for the top 5 (stdlib HTMLParser, 3000 chars); catalyst inference
  reads scraped text, not just headlines. StockTwits with null-safe sentiment.
  Writes `logs/viral_research/` + `logs/viral_research_summary.csv`.
  `get_catalyst()` is a cheap file read used as an entry gate.
- `strategies/` — double_bottom (W-pattern; **early entry at the second bottom** is the
  primary mode since 2026-07-06: tie-tolerant pivots 5-left/3-right, green bar closing
  ≥0.5 ATR above Low2 while below halfway to the neckline, Low2 may sit up to
  max(2%, 2 ATR) ABOVE Low1; sets `ignore_vwap=True` since W bottoms are below VWAP
  by nature; PULLBACK-RETEST mode added 2026-07-07 (SKIN): if the neckline broke
  within the last 30 bars and price dipped back below it while holding the upper
  half of the W, buy a green bar bouncing 0.5 ATR off the pullback low, stop 0.5 ATR
  under that low — the "buy the dip after the break" entry the owner wants;
  the old neckline-break entry remains as fallback; W MAX-DEPTH CAP added
  2026-07-13 (SOBR): reject patterns where neckline - low1 > 20% of Low1
  (`DB_MAX_DEPTH_PCT`, pct-only on purpose — an "or N ATR" escape would
  exempt exactly the parabolic spikes it targets). Without it a blow-off
  top gets read as the "neckline" and the whole spike-and-collapse as one
  giant W (SOBR 07-13: 24%-deep "W", neck 0.838 was the 10:20 parabolic
  peak, -5.4%; JZXN 07-10: 52%-deep, -10.2%); ALL modes additionally
  require the Stoch(5,3,3) second-bottom gate — %K turning up off a fresh
  <=30 trough, %K <= 55, added 2026-07-07 after BJDX bought a range-top W at
  K=57 — see validation), trend_reversal (the
  volume producer), resistance_breakout (2+ touch tie-tolerant levels; entry band
  widened to 0.2–2% above the level and entries allowed within 3 bars of the first
  closing break — the old 0.5–1% band was one candle wide and never filled; same
  retest mode as double_bottom: a break up to 30 bars old is NOT spent if price
  pulled back within 2 ATR of the level — buy the bounce, stop under the pullback low).
  Signals with score ≥ 3 trigger; double_bottom and resistance_breakout set their
  own stop/target overrides. gap_bounce exists but is **retired from the live loop**
  (kept only in backtesting/engine.py).
- `trading/portfolio.py` — per-ticker daily caps (3 trades, 2 stops), 30-min cooldowns
  after any exit, state restored across restarts (incl. closed_today so caps survive
  AND open positions with their full stop/trail/take-profit structure — positions
  were saved but never restored until 2026-07-14, so restarts silently fell back
  to main.py's broker sync with a generic 2%-ATR stop). state.json writes are
  atomic (temp file + os.replace). SQLite was evaluated and deliberately
  REJECTED for now (2026-07-14): EOD-flat bot, one RLock-guarded writer,
  atomic JSON covers crash safety — revisit at IBKR go-live for order/fill
  tracking, not before.
  Every closed LIVE trade is also appended to `logs/trade_outcomes.csv`
  (append-only, survives the nightly state reset; same schema as the
  backtest trade CSVs so live+backtest join on date+ticker vs research) —
  added 2026-07-13 so real paper-trading outcomes accumulate for strategy
  refinement (state.json alone is wiped daily; .md reports aren't queryable).
- `main.py` — entry gates in order: after 10:00 ET, before 15:30 ET, catalyst != "unknown",
  close ≥ session VWAP, caps/cooldowns. Exits: trailing stop 0.75R below high-water,
  armed once +1R is reached (no fixed take-profit). Exits run in a DEDICATED
  THREAD (`exit_monitor_loop`, every 5s) so client-side stops never wait
  behind a slow watchlist scan; `Portfolio.begin_close()` claims a ticker so
  the exit thread and EOD close can't double-sell (2026-07-14). EOD close
  RETRIES every tick until the portfolio is flat — a failed 15:30 sell used
  to be forgotten and could leave an overnight position. check_entries is
  wrapped in an exception boundary; all per-ticker work is inside the
  per-ticker try. Live bars DROP the still-forming minute candle so live
  signal timing matches the backtest (completed candles only). The trail used to start at +1.5R
  with only breakeven in between — a winner peaking between +1R and +1.5R round-tripped
  to zero (SOXS 2026-07-07: +5.1% peak exited -0.2%); fixed 2026-07-07. **All positions
  closed at 15:30 ET**, then the daily report saves to `logs/reports/`.

## Non-obvious constraints — don't "fix" these away
- **IEX volume is ~2-3% of real volume.** SCAN_MIN_VOLUME=300_000 on IEX ≈ 10M+ real
  shares. If the data feed ever changes to SIP, this and the strategy volume gates must
  be recalibrated ~30-50x upward.
- **Windows console is cp949 (Korean).** No em-dashes/fancy Unicode in anything logged —
  it crashes console logging. Plain ASCII in log messages.
- The catalyst gate skips only literal "unknown" — missing research (None) is allowed
  through so a slow research thread doesn't block trading.
- SCAN_PRICE_MIN=0.5 was deliberately lowered from 1.0 (owner wants sub-$1 names like BIYA).
- **Strategies require MIN_BARS=45, not a full 120-bar window.** Tickers with no
  premarket bars only have ~80 bars by 11:00 ET; the old `len(df) < WINDOW` check
  silently disabled double_bottom AND resistance_breakout all morning (found via
  LUCY 2026-07-06: bot bought the neckline at $1.18/11:00 instead of the second
  bottom at ~$1.10/10:50 because the detector returned insufficient_bars).
- **Only one bot instance may run** — main.py binds localhost port 8049 as a
  mutex and exits if it's taken. Added 2026-07-08 after a stale manual run and
  the 6:30 scheduled task traded the same account simultaneously (BIYA bought
  twice; each instance blind to the other's position). Don't remove the lock,
  and don't leave a manual `py -3.12 main.py` running overnight — the
  scheduled task fires at 6:30 Mon-Fri.
- **get_quote must never return the raw IEX midpoint.** On thin sub-$1 names
  the IEX book goes one-sided and the midpoint collapses (BIYA 2026-07-08:
  stop at $0.48 "hit" 11s after a $0.5085 entry while prints were $0.505+ —
  two instant false stops in one morning). Order: latest IEX trade, then
  midpoint only if two-sided with spread <= 5%, then yfinance.
- Pivot detection must tolerate ties (PIVOT_TIE_PCT): penny bottoms double-print
  the exact same low and resistance IS repeated equal highs — strict `<`/`>`
  comparisons reject the very patterns being hunted.
- The VWAP gate is per-signal (`Signal.ignore_vwap`), not per-ticker. Early W
  entries set the flag — don't hoist the gate back above the strategy loop or
  they die. Owner first wanted VWAP enforced on early entries, then approved
  waiving it (2026-07-06) after the backtest showed below-VWAP bottoms carry
  the edge (PF 1.37 waived vs 1.08 enforced; see validation below).

## Review fixes + parameter freeze (2026-07-14)
External code review (two rounds) confirmed four strategy bugs, all fixed:
resistance_breakout searched for the level break from touches[0] (a break
before the level existed) -> now touches[-1]+1; double_bottom breakout target
was close+(neck-low1) -> now neckline-based like the other modes; trend_reversal's
prior-downtrend is now a HARD GATE (was +1 point; the other components are
correlated echoes of the same bounce); Low2 higher-low tolerance capped at 5%
absolute (2-ATR clause was unbounded on violent names). Execution hardening:
EOD retry-until-flat, exit-monitor thread, exception boundaries, position
restore, atomic state writes, forming-candle drop, chase guard, risk-overrun
logging. Post-fix in-sample backtest: 124 trades, +$27.3K, PF 2.39, 56% WR,
$221/trade (down from PF 2.75/+$33.4K pre-fix — expected: part of the old
edge was logically invalid signals; the drop is mostly trend_reversal's hard
gate and it was NOT tuned back). double_bottom 28 trades +$11.4K, resistance_
breakout 60 trades +$8.0K (touches[-1] fix doubled its valid levels),
trend_reversal 36 trades +$7.9K. **Parameters are FROZEN as `config.STRATEGY_VERSION`
(us-penny-v1.0-frozen-2026-07-14). Do NOT add new filters after individual
losing trades — the June/July backtest sample is exhausted as design data;
paper trading from this version forward is the out-of-sample test. Bump the
version string if logic changes so forward-test samples don't mix.**

## Validation status (as of 2026-07-06)
- **1-year backtest** (`backtest_1y.py`, 2025-07→2026-07, 2,419 trades on historical
  viral pennies, SIP data): +$357.7K on $100K, PF 1.53, 40% WR, avg win/loss 2.3:1,
  **all 12 months positive**. Trailing stops are the entire edge (+$937K gross vs
  -$628K in stops). All three strategies net positive. **Run with the PRE-2026-07-06
  entry logic** — needs a re-run (clear the logs/reports checkpoint files first) to
  validate the early-W/wider-breakout changes at scale.
- **Viral backtest after the 2026-07-06 entry changes** (`backtest_viral.py`),
  three variants on the same universe:
  - Original neckline-only logic: 37 trades, +$5.1K, PF 1.51
  - Early W entry, VWAP waived (CURRENT, `ignore_vwap=True`): 96 trades,
    +$12.8K, PF 1.37, 43% WR, expectancy $133/trade, double_bottom +$7.1K
  - Early W entry, VWAP required: 70 trades, +$2.5K, PF 1.08,
    **double_bottom -$6.6K** (12/39 wins)
  - Interpretation: below-VWAP early W entries were the profitable ones (tight
    stop near the bottom); requiring VWAP reclaim drops those winners and keeps
    the weak above-VWAP patterns. Owner chose the waived variant 2026-07-06.
    resistance_breakout fires in all new variants (~8-9 trades, +$4K, was zero
    due to the one-candle-wide entry band).
- **Viral backtest after the 2026-07-07 changes** (retest entries + trail armed
  at +1R instead of +1.5R): 107 trades, +$14.0K, PF 1.38, **53% WR** (up from
  43% — the earlier trail converts +1R..+1.5R peaks into wins instead of
  breakeven round-trips), expectancy $131/trade; double_bottom +$10.3K,
  trend_reversal +$2.4K, resistance_breakout +$1.3K. Note: universe had grown
  by ~1 day (2026-07-07 tickers incl. SKIN/SOXS) vs the 96-trade baseline, so
  the comparison is close but not exactly apples-to-apples.
- **Stochastic gate (2026-07-07, BJDX):** gating ALL double_bottom entries on
  Stoch(5,3,3) %K turning up off a <=30 trough within 3 bars while %K <= 55
  (`DB_STOCH_GATE=turn`, the default): 98 trades, **+$25.5K, PF 2.20, 58% WR,
  expectancy $260/trade** — vs $131 ungated. double_bottom itself: 46 trades
  +$17.6K (was 76 trades +$10.3K); blocked mid-range entries free the ticker
  slot so resistance_breakout (20 trades) and trend_reversal (32) fire more.
  The full stoch-W variant (`w`) scored the same (PF 2.23) — not worth the
  extra complexity. BJDX 2026-07-07 14:59 (bought K=57 mid-range at range
  top, -4.6%) is the motivating trade; the gate blocks all 7 of its
  trigger bars. A STRICTER gate (trough<=25, K<=45) was tested 2026-07-08
  and REJECTED: PF 1.94, double_bottom +$11.7K -> +$4.9K — it drops more
  winners than losers. Don't tighten it again without a backtest.
- **Resistance-capped take-profit (2026-07-08, VTAK):** double_bottom caps
  its target at the nearest overhead 2+ touch swing-high zone (tolerance
  max(2%, 2 ATR), conservative bottom of the zone; `find_overhead_resistance`
  in resistance_breakout.py) and SELLS there (`Signal.take_profit=True` ->
  real take-profit exit, unlike normal advisory targets). Trades with capped
  reward < 0.5R are skipped ("w_no_room"). Motivating trade: VTAK 2026-07-08
  12:25, target 1.65 above a ~1.47 wall tagged twice, rode to -9.4%.
  Backtest with cap: 110 trades, **+$27.0K, PF 2.24, 59% WR, $245/trade**
  (vs +$25.5K / PF 2.20 uncapped); take_profit exits 11 trades +$6.8K.
- **Stoch "deeper" gate (2026-07-14, AGEN):** owner rule — the fresh stoch
  trough must be at least as deep as the lowest %K of the prior 30 bars
  (+5 pts tolerance; `DB_STOCH_GATE=deeper`, `DB_STOCH_DEEPER_TOL=5`, now
  the default). A shallower dip than the last one is a fake second bottom
  (AGEN 07-13 11:41, -2.8%). Sweep: turn PF 2.28/$231 -> deeper5
  **PF 2.75, $261/trade, 61% WR, +$33.4K on 128 trades**; tol=0
  over-tightens (PF 2.46). Caveat: IEX vs consolidated stoch values
  differ on thin names — the gate sees IEX.
- **Entry chase guard (2026-07-13, FTRK):** market buys are skipped if the
  live quote runs >1.5% above the signal price (`MAX_ENTRY_CHASE_PCT`).
  FTRK 14:29: signal 0.6332 (legal, 1.4% over the 0.6247 level) but the
  market order filled 0.66 (+4.2%) into a vertical spike, then -6.9%.
- **W max-depth cap (2026-07-13, SOBR):** sweep of DB_MAX_DEPTH_PCT on the
  grown universe (13 sessions, 06-26→07-13): uncapped 145 trades +$30.0K
  PF 1.94 / cap 0.10 → PF 2.12 but drops winners / cap 0.15 → PF 2.13 /
  **cap 0.20 → 132 trades +$29.9K PF 2.18, 58% WR, $227/trade (chosen)** —
  same total PnL as uncapped on 13 fewer mostly-losing trades, double_bottom
  +$7.6K → +$9.5K. Blocks SOBR (23.4% deep) and JZXN (52%). Don't tighten
  below 0.20 without a new sweep — 0.10 already cuts winners.
- **Flat-base hard gate experiment (2026-07-14, VMAR):** first out-of-sample
  loss under v1.0 — trend_reversal bought VMAR 10:00 at the top of a
  parabolic spike (+48% over prior close, right under a 2.088 shelf) and
  stopped -10.5%. The premarket fade read as "prior downtrend"; the flat/
  sideways component of the strategy is only +1, not required, and VMAR
  scored 3 without it. Owner rule: reversal should need sideways
  consolidation first. Tested via `TR_REQUIRE_FLAT=1` (env flag, default 0):
  the 2%-range flat base NEVER occurs on viral pennies — trend_reversal
  went to ZERO trades. Same-universe comparison (117 ticker-days):
  baseline 125 trades +$26.3K PF 2.28 vs gated 100 trades +$21.9K PF 2.69
  (stop bleed -$20.6K -> -$12.9K; freed slots pushed double_bottom 28->33
  trades +$2.4K). So the honest choice is binary: keep trend_reversal
  as-is or retire it like gap_bounce — a softer flat band would be a new
  knob tuned on exhausted data. DECISION PENDING with owner; v1.0 stays
  frozen with the flag at 0 meanwhile. Also on this trade: quote 2.03
  passed the 1.5% chase guard but the market order filled 2.09 —
  risk_overrun_pct logged +59.1%; if overruns recur, the fix is limit
  orders, not filters. The recovery W at 10:25-10:30 was missed because
  of the 30-min stop cooldown (10:10 stop locked the ticker to 10:40),
  not signal logic.
- Known biases, all optimistic: no slippage (edge is 0.75%/trade vs penny spreads
  0.3-1% — real results likely ~half), survivorship (delisted names missing),
  no cross-ticker MAX_POSITIONS cap in the sim.
- **Current phase:** paper-trade 2-3 weeks (~100 trades) and compare live expectancy
  vs the backtest's +$148/trade. Watch whether resistance_breakout ever fires live
  (0 trades in the 5-day backtest; may be over-strict). Fund only if paper holds up.

## Go-live plan (decided, not yet started)
- Webull Canada has **no API** (app-only) — that's why execution is Alpaca paper now.
- Live path: IBKR Canada (account already opened). Only `trading/broker.py` needs an
  IBKRBroker class (~150 lines, ib_insync); scanner/data stay on Alpaca. Margin account,
  US stocks permission. **PDT rule is GONE** — FINRA retired it 2026-06-04 (all US
  brokers, IBKR included) and replaced it with an intraday margin framework: no
  day-trade count limits at any equity level, 4x intraday buying power now needs only
  USD $2K equity (was $25K), pre-trade checks reject orders exceeding intraday margin,
  and repeated unmet intraday margin calls (5 business days) can freeze the account
  ~90 days. The old $25K barrier to going live no longer exists. Alpaca deprecated the
  PDT API fields (pattern_day_trader, daytrade_count, daytrading_buying_power, ...) —
  audited 2026-07-07, the bot never used them (sizing works off account equity).
- At go-live, upgrade data: IBKR bundle (~$10/mo) or Alpaca SIP ($99/mo, zero code change).
