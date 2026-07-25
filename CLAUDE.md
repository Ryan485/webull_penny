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
  own stop/target overrides. trend_reversal was REWRITTEN 2026-07-14 (owner
  directive after VMAR): the old MA-cross/stoch/RSI scoring version is gone;
  it now detects structural bullish reversals by NECKLINE — inverse H&S /
  rounding bottom / cup & handle share one skeleton (prior downtrend hard
  gate -> window-low base -> rally high = neckline -> higher right-side low
  -> neckline breakout or 30-bar retest entry), with the same 4%-min/20%-max
  base-depth caps and resistance-capped take-profit as double_bottom.
  Triple bottom is deliberately absent (double_bottom fires on its last two
  lows, resistance_breakout on the box top); bullish wedge excluded per
  owner. gap_bounce exists but is **retired from the live loop**
  (kept only in backtesting/engine.py).
- `trading/portfolio.py` — per-ticker daily caps (3 trades, 3 stops — raised
  from 2 stops 2026-07-16 after TGHL; the trade cap is the binding limit now),
  10-min cooldowns
  after any exit (30 -> 10 in v1.3), state restored across restarts (incl. closed_today so caps survive
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
- SCAN_PRICE_MIN=1.0. Was lowered 1.0->0.5 earlier (owner wanted sub-$1 names
  like BIYA), then RAISED BACK 0.5->1.0 on 2026-07-21 (v1.5) once fees were
  modeled: sub-$1 names pay ~1.5% round-trip IBKR commission plus the widest
  spreads, and the sim showing them net-positive is the least trustworthy slice
  (zero spread + survivorship). See the fee/price-floor entry below.
- **Live P&L is now NET of IBKR commissions (2026-07-21).** `config.ibkr_commission`
  models the IBKR Fixed US-stock tier ($0.005/share, $1 min/order, 1% of value
  cap) and `Portfolio.close_position` subtracts a round trip (buy+sell) from
  every trade so `pnl`/`pnl_pct` and the account curve reflect real go-live
  costs; the fee is stored per-trade (`commission` col, appended to
  trade_outcomes.csv via a one-time header migration). CONSEQUENCE for the
  strategy, not yet acted on: the per-share fee is brutal on sub-$1 names
  (10k-20k shares) — a $0.53 name pays ~1.9% round trip, larger than the
  ~0.75% edge, so cheap tickers are structurally negative before being right
  or wrong (KIDZ 07-21 +$40.52 gross winner -> -$115 net). $2+ names pay
  ~0.2-0.4% and are unaffected. RESOLVED 2026-07-21: SCAN_PRICE_MIN raised
  0.5 -> 1.0 (v1.5). Net-of-fees backtest by entry-price bucket: <$1 17
  trades +$3.7K (fee 1.53%), $1-2 39 +$7.7K (0.73%), $2-3 32 +$5.6K, $3-5 35
  +$7.9K, $5+ 24 -$2.1K (fee only 0.17% -- a SIGNAL problem, not a fee one).
  Sub-$1 was still net-positive in the sim, but that slice is the least
  trustworthy (zero spread + survivorship flatter the delisting-prone cheap
  names), so $1.00 removes it while keeping the clean $1-2 bucket; $2.00 was
  rejected (throws away ~$11K of real profit). Owner directive: total return
  is the goal, and cutting the profitable $1-2 names to chase fees would hurt
  it. The backtest (`backtest_viral.py`) now
  subtracts the SAME fee on both legs (owner directive 2026-07-21), so it is
  apples-to-apples with live-net: fee-adjusted baseline dropped to 147 trades
  +$22.8K, PF 2.04, $155/trade (from gross +$37.8K/PF 3.53/$274) — fees roughly
  HALVE the modeled edge (and notably land near the original 1y +$148/trade
  figure). Still zero SLIPPAGE in the sim, so the real number is thinner again.
  The fee-accounting change ALONE would not bump STRATEGY_VERSION (signals
  unchanged); the accompanying SCAN_PRICE_MIN 0.5->1.0 universe change does ->
  v1.5.
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
trend_reversal 36 trades +$7.9K. **Parameters are FROZEN as `config.STRATEGY_VERSION`.
Do NOT add new filters after individual losing trades — the June/July backtest
sample is exhausted as design data; paper trading from this version forward is
the out-of-sample test. Bump the version string if logic changes so forward-test
samples don't mix.** v1.0 (us-penny-v1.0-frozen-2026-07-14) lasted one live
trade (VMAR): superseded same day by **us-penny-v1.1-structural-tr-2026-07-14**
(trend_reversal rewritten as a structural neckline detector on owner
directive — see the flat-base experiment entry in Validation). v1.1
same-universe backtest: 103 trades, +$24.0K, PF 2.90, 60% WR, $233/trade.
**us-penny-v1.2-stopcap3-2026-07-16**: per-ticker daily stop cap raised
2 -> 3 (see the TGHL stop-cap entry in Validation) — no signal-logic change;
v1.2 baseline on the grown universe (incl. 07-15/07-16): 126 trades,
+$28.5K, PF 2.81, 58% WR, $226/trade. **us-penny-v1.3-cooldown10-2026-07-16**
(same day, owner "remove the cooldown" directive): both per-ticker exit
cooldowns cut 30 -> 10 min (see the cooldown sweep entry in Validation);
v1.3 baseline: 130 trades, +$34.9K, PF 3.32, 62% WR, $268/trade.
**us-penny-v1.4-dbgap60-2026-07-20** (owner directive after SLNH): double_
bottom `MAX_GAP` cut 90 -> 60 bars so the two lows can't sit 2.5h apart (see
the W max-gap entry in Validation); v1.4 baseline on the grown universe:
138 trades, +$37.8K, PF 3.53, 62% WR, $274/trade.
**us-penny-v1.5-pricefloor1-2026-07-21**: two coupled changes (owner
directive after the KIDZ 07-21 fee finding) -- (1) live+backtest P&L is now
NET of IBKR Fixed-tier commissions (`config.ibkr_commission`, see the fee
bullet under Non-obvious constraints), and (2) SCAN_PRICE_MIN raised 0.5 ->
1.0 to drop the sub-$1 names the fee+spread tax hits hardest. No signal-logic
change. Net-of-fees baseline (before the floor change) was 147 trades +$22.8K
PF 2.04 $155/trade; the $1 floor removes the <$1 bucket (+$3.7K sim, least
trustworthy slice) while keeping the profitable $1-2 names. From here the
forward-test scoreboard is net-of-fees -- do NOT compare live to the old
gross +$37.8K figure.
**us-penny-v1.6-stopfloor3-2026-07-24**: ONE change -- a new
`config.MIN_STOP_PCT` = 3% minimum stop distance, applied in main.py before
sizing and mirrored in backtest_viral.py. Motivated by the first full month of
live data (28 trades, -$5,871 net): the structural stop had a MEDIAN distance
of 1.53% while the median dip a trade had to survive before running was 3.9%,
so 67% of stopped-out trades recovered above our entry. Raising STOP_ATR_MULT
0.5 -> 1.0 was tested at the same time and REJECTED (made it worse; ATR-scaling
is what fails on thin IEX names). See the month-1 review entry in Validation.

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
  trades +$2.4K). RESOLVED same day: owner rejected both retiring and the
  flat gate — directive was "trend reversal is good only when you do it
  correctly: track the resistance level and neckline" (chart-pattern
  reference: iH&S / rounding bottom / cup & handle; no bullish wedge).
  Strategy rewritten structurally (see Architecture) as **v1.1**:
  same universe, 103 trades +$24.0K **PF 2.90, 60% WR, $233/trade**;
  trend_reversal itself 3 trades 3/3 wins +$1.5K (2 retest + 1 breakout),
  VMAR 07-14 produces ZERO trades under the new logic. Beats both
  keep-old (PF 2.28) and retire (PF 2.69). Only 3 in-sample trades =
  thin evidence; live forward test judges it. The TR_REQUIRE_FLAT flag
  was removed with the rewrite. A second external review of the rewrite
  (2026-07-14) was triaged: its "look-ahead bias" claim is WRONG (the
  backtest replays evaluate() bar-by-bar on completed candles, same as
  live — pivots cannot appear until confirmed); entry-candle volume
  (not break-candle) is the deliberate rb-matching convention; the one
  accepted fix was the retest hold gate — pullback must hold the upper
  half between right low and neckline (RETEST_MIN_HOLD=0.5, matching
  double_bottom), which swapped BMNU's deep retest for its clean 11:48
  breakout (+$973). Retest-recency and fixed downtrend-lookback ideas
  were deferred to forward testing (tuning knobs on exhausted data).
  Also on this trade: quote 2.03
  passed the 1.5% chase guard but the market order filled 2.09 —
  risk_overrun_pct logged +59.1%; if overruns recur, the fix is limit
  orders, not filters. The recovery W at 10:25-10:30 was missed because
  of the 30-min stop cooldown (10:10 stop locked the ticker to 10:40),
  not signal logic.
- **rb-retest VWAP waiver tested and REJECTED (2026-07-14, LHAI):** LHAI's
  box-top retest at ~12:20 (level 1.252, price 1.27, VWAP 1.311) was the
  entry the owner wanted; the per-signal VWAP gate blocks it. Tested
  waiving VWAP for rb retest mode only (`RB_RETEST_IGNORE_VWAP=1`, env
  flag, default 0): PF 2.90 -> 2.22, +$24.0K -> +$18.8K — the 12 extra
  below-VWAP retests lose net AND displace better double_bottom entries
  (33 -> 30 trades). Below-VWAP entries work for W bottoms, NOT for box
  retests. Don't re-enable without new out-of-sample evidence. The real
  LHAI blocker was IEX data starvation anyway: 10-18 min gaps between
  prints (89 stale-data warnings 11:25-11:49 ET), unfixable in code —
  SIP data at go-live. The owner's "box" rule (level tested multiple
  times) is already rb's core requirement (2+ touches, 0.5% cluster,
  >=10 bars apart).
- **3+ touch minimum tested and REJECTED (2026-07-14):** owner intuition
  was that 2 tests of a level is too few. Sweep via `RB_MIN_TOUCHES`
  (env flag, default 2): requiring 3+ collapses rb to 20 trades,
  +$196 TOTAL, 35% WR (from 67/+$8.1K/51%); whole book +$24.0K ->
  +$14.3K, PF 2.90 -> 2.49. The extra-tested levels are the WORSE
  trades — on 1m viral pennies a box tested many times means heavy
  supply sitting on the level and a late entry in the window; the
  profitable core is the fresh 2-touch level breaking early. Don't
  raise it. (Overhead-resistance TARGET capping keeps 2 touches
  independently — for selling, a twice-tagged wall still counts.)
- **Stop-cap raise 2 -> 3 ADOPTED as v1.2 (2026-07-16, TGHL):** TGHL took
  two stops (12:03 rb -2.2%, 12:47 db -3.7%) and the 2-stop blacklist then
  blocked rb's 14:28 ET breakout signal (res=1.445, 7x vol) right before
  the run 1.46 -> 1.576 — replay confirmed the bot SAW the entry and the
  cap alone refused it. Sweep via `BT_MAX_STOPS_PER_DAY`: cap 2 gives 121
  trades/+$25.1K/PF 2.62; cap 3 gives 126/+$28.5K/PF 2.81/$226 per trade;
  uncapped is IDENTICAL to cap 3 (the 3-trades/day cap binds first, so
  cap 3 = blacklist removed). Only 5 extra trades = thin, but direction +
  owner directive agree. Churn guards remaining: 3 trades/ticker/day +
  30-min stop cooldown. Related: the 12:40 db entry looked "stoch ~60,
  mid-range" on Webull but was K=31.7 off a 0.0 trough on IEX bars (IEX
  printed NO bars 12:33-12:38) — consolidated-vs-IEX stoch gap again, and
  that W genuinely failed (lower low 1.31 at 12:47), so the stop itself
  was correct. The owner's sell-at-resistance / re-enter-on-break rule is
  already implemented (resistance-capped take_profit + rb re-entry); the
  30-min PROFIT cooldown is the remaining friction — untested, candidate
  for a future sweep, don't change without one. (Swept same day — see the
  cooldown entry below; both cooldowns are 10 min as of v1.3.)
- **Cooldown 30 -> 10 min ADOPTED as v1.3 (2026-07-16, owner directive):**
  owner asked to REMOVE the post-exit cooldown ("if you spot a good
  opportunity, keep trying"). Sweep via `BT_COOLDOWN_MINS` (applies to all
  exits): 30 min = 126 trades/+$28.5K/PF 2.81; 0 min = 132/+$31.1K/PF 2.78
  (avg loss grows -$297 -> -$329: instant re-entries into still-falling
  names); **10 min = 130 trades/+$34.9K/PF 3.32, 62% WR, $268/trade —
  best on every metric, adopted for BOTH stop and profit cooldowns.**
  A short breather filters the machine-gun re-buys while still catching
  recovery setups the 30-min lock missed (VMAR 10:25 W on 07-14, TGHL
  12:33 window on 07-16). Full removal was NOT adopted: it is worse than
  10 min everywhere and matches the owner's own "not without strategy"
  caveat. Churn guards now: 3 trades/ticker/day + 10-min exit cooldowns.
- **Later EOD hold tested and REJECTED (2026-07-17, LCID):** LCID rb entry
  15:26 was force-flattened at 15:30 (+$43) and the stock then spiked to
  7.56 — owner asked why a working trade gets dumped instead of managed.
  Sweep via `BT_EOD_HHMM` (env flag, default 1530; entry cutoff stays a
  separate 15:30 constant): hold-to-15:45 = +$36.3K PF 3.23; hold-to-15:55
  = +$35.5K PF 3.15 vs baseline 15:30 = +$36.6K PF 3.36 (138 trades, 62%
  WR, $265/trade on the grown universe). Only 5 of 138 trades are open at
  15:30 at all, and holding them later made 4 of 5 WORSE (SUNE gave back
  $588 of a $706 win; CPHI turned a flat close into a -$556 stop; NNOX
  doubled its loss; SOXS gave back $195; only ILLR improved, +$718 via
  its capped take-profit) — net -$1.1K. Viral pennies fade into the
  close; 15:30 sells the fade's top on average. LCID 07-17 itself is the
  exception, not the rule (and the sim never reached its 15:26 entry —
  the 3-trades/day cap was already spent by 13:35). Thin sample (5
  trades), so re-sweep if more late-day winners accumulate live; the
  flag stays for that. Late-day liquidity/slippage would only make
  holding look worse than the no-slippage sim shows.
- **W max-gap 90 -> 60 ADOPTED as v1.4 (2026-07-20, SLNH):** double_bottom
  paired SLNH's 11:14 low with its 13:44 low (86 bars / 2.5h apart) and
  called the intervening chop a "W" -> bought 14:40, stopped -2.9%. Owner
  rule: "a double bottom's two lows shouldn't be that far apart -- use the
  stochastic to spot it; the two troughs should be close, like NUAI's two
  boxes ~25 min apart." `MAX_GAP` (Low1->Low2 bar distance) is the lever;
  swept via `DB_MAX_GAP`: 90 (frozen) 149 trades/+$37.8K/PF 3.26 vs
  **60 138 trades/+$37.8K/PF 3.53/$274 per trade (chosen)** -- same PnL on
  11 fewer near-breakeven pairs, and SLNH's bad W is eliminated entirely
  (a smaller rb_retest -$95 takes its slot vs the db -$382). Below 60
  falls off a cliff (45 -> PF 2.70, 30 -> PF 2.63: both start cutting
  genuine quick-bounce winners), so 60 is the floor, not lower. double_
  bottom itself 54 -> 40 trades, +$19.57K -> +$19.27K (the -$0.3K is all
  dropped junk). MIN_GAP stays 20. The stochastic already gates the SECOND
  bottom (deeper mode); requiring the FIRST low to be a stoch trough too (a
  full stochastic W on BOTH boxes) is the deferred next lever if wide-but-
  legal pairs still slip through live -- not implemented, would need its
  own sweep. Note: 07-20 SLNH is in-sample now, so this is design data, not
  an out-of-sample confirmation.
- **rb overhead-resistance cap tested and REJECTED (2026-07-20, AMC):** AMC
  07-20 lost -$631 across three rb stops; the 13:44 one (retest of the 2.38
  level, bought 2.435 right under the unbroken 2.44 all-time-high wall, blind
  2R target 2.588 sat ABOVE it, -3.3%) prompted owner fix: give rb the
  overhead-resistance target cap double_bottom already uses. Implemented behind
  `RB_OVERHEAD_CAP` (off/skip/cap, default off = frozen; NO version bump).
  Note find_overhead_resistance returns None here -- its conservative zone-
  BOTTOM (2.43) sits under the 2.435 entry -- so the cap uses rb's OWN level
  list (level = max touch high = 2.44). Sweep: off 138 trades/+$37.8K/PF 3.53,
  rb +$15.8K; **skip** (refuse entries with <0.5R room to the wall, keep 2R+
  trail otherwise) 137/+$36.6K/PF 3.36, rb +$14.6K; **cap** (sell at the wall)
  137/+$33.6K/PF 3.17, rb +$11.7K. BOTH hurt. cap guillotines the fat right
  tail (-$2,941 over 6 capped trades: IFRX +$2,494 trailing win -> +$162 sell-
  at-wall; NNBR -$748) because momentum pennies BLOW THROUGH resistance -- that
  IS rb's edge (trailing stops are the whole edge per the 1y backtest). Even
  the surgical skip-only version costs -$1.2K/PF -0.17: the 6 "jammed under a
  wall" trades it removes were net WINNERS (+$1.3K, PRME +$931) because the
  walls didn't hold. Owner intuition (don't buy into the wall) is right for the
  single AMC trade but contradicted across the sample -- on viral names walls
  are speed bumps, not ceilings. AMC 07-20 isn't in the universe (would need a
  re-sweep once it is). Default stays off; flag kept for a re-sweep if live rb
  shows systematic sell-into-wall losses. Don't enable without new evidence.
- Known biases, all optimistic: no slippage (edge is 0.75%/trade vs penny spreads
  0.3-1% — real results likely ~half), survivorship (delisted names missing),
  no cross-ticker MAX_POSITIONS cap in the sim.
- **MONTH-1 LIVE REVIEW (2026-07-24, 28 trades 07-14 -> 07-24, -$5,871 net,
  25% WR, PF 0.16).** Owner asked for a full review of the live logs. Findings,
  all measured on real fills + real 1m bars (single-trade replay, not universe
  sweeps):
  1. **Losses match the model; wins do not.** Avg loss -$334 live vs -$347
     modeled (fine), but avg win +$162 vs +$531 modeled. Win/loss ratio 0.49:1.
     Stop-out rate 64% live vs 37% in the sim; trailing-stop wins +1.01% live
     vs +3.07% sim. Signals find the moves - the exits give them away.
  2. **The stop sat inside the noise.** Median structural stop 1.53% of entry;
     median dip a trade had to survive before running 3.9%. Of 18 stopped-out
     trades, **12 (67%) recovered back above our entry** and 6 would have hit
     the full 2R target. TGHL stopped in 2 min then ran +19.4%; KIDZ 1 min then
     +19.4%; EHGO 2 min then +13.2%. -> fixed as v1.6 (MIN_STOP_PCT).
  3. **Execution costs about half the loss:** commissions $1,554 + exit
     slippage $1,452 (avg 0.72% worse than the stop price; VMAR -2.8%,
     EHGO -2.8%) = ~$3,006 of the $5,871. Fix is LIMIT orders, not filters -
     deferred, do it as its own change.
  4. **Signal quality differs sharply by strategy** (MFE/MAE on real bars,
     independent of exit rules): double_bottom median MFE +10.3% vs MAE -3.6%
     (2.9:1, real edge, 6/10 ran >5%); resistance_breakout median MFE +1.4%
     vs MAE -2.2% (0.6:1) with 7 of 17 never running even +1% - and rb is 61%
     of all live trades. Not an AMC artifact (median identical excluding it).
     The backtest agreed directionally ($71/trade rb vs $322 db). Owner
     decision 2026-07-24: **do NOT cut rb yet** - 17 trades is too thin
     (margin of error ~+/-12 pts on win rate at n=17; ~50-100 needed), paper
     trading is free, so keep collecting. Revisit at ~50 rb trades.
  5. **The 20% cost cap binds on 28/28 trades** - i.e. position size is set by
     notional (~$10.5K), NOT by risk, so `calc_position_size`'s 2% risk rule
     never binds and actual risk has been only ~0.4% of account. CONSEQUENCE:
     a wider stop does NOT reduce share count, so it raises dollar risk per
     trade (~$202 -> ~$342 under v1.6) and does NOT reduce commissions. Any
     future analysis that assumes "wider stop = fewer shares = same risk" is
     WRONG for this bot. Sizing itself is a live open question, untouched.
- **Stop noise-floor ADOPTED as v1.6 (2026-07-24):** `config.MIN_STOP_PCT`=3%,
  applied in main.py before sizing (mirrored in backtest_viral.py). Swept on
  the 28 real live entries using the REAL sizing function: baseline -$3,569
  (7W) -> floor 3% **-$2,160 (11W)** -> floor 4% -$2,081 (11W). Raising
  STOP_ATR_MULT 0.5->1.0 instead/as well was REJECTED (mult only -$3,615;
  mult+floor -$2,651, i.e. worse than floor alone) - ATR-scaling is precisely
  what breaks on thin IEX names where ATR is understated (ATAI 0.22% ATR gave
  a 0.60% stop). Flat percentage floor compensates for a known feed bias.
  **CAVEAT - the evidence conflicts:** the same change makes the viral
  BACKTEST worse (+$22.8K/PF 2.04 -> +$17.5K/PF 1.49, avg loss -$347 ->
  -$608). The sim models ZERO slippage and perfect stop fills, so it cannot
  see the harm a too-tight stop causes and only sees the bigger losses - it is
  structurally biased toward tight stops. The live replay is the more relevant
  evidence for this specific question, but n=28 is small. The forward test
  settles it; if live does not improve, revert this first.
- **Current phase:** forward-test v1.6 on paper. Watch (a) whether stop-outs
  fall from 64%, (b) whether avg win rises from $162 toward the modeled $531,
  (c) resistance_breakout trade count toward ~50 for the keep/cut decision.
  Scoreboard is NET of fees - do not compare to the old gross figures.

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
