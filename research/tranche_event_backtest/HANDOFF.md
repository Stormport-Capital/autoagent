# Handoff: tranche event backtest (cloud session → Dean's PC)

Branch `claude/autoagent-backtest-report-lrmwqk`, draft PR
https://github.com/Stormport-Capital/autoagent/pull/5. Written 2026-10-02 at
the end of the cloud session. Every decision Dean made is recorded below.
Do not re-ask anything answered here; the items under "Open for Dean" are the
only open questions.

## What this is

A strategy comparison, not machine learning: no held-out period, no tuning,
no parameter changes. Four autoagent tranche-dashboard entries (5/10 EMA
cross, 10/20 EMA cross, VWAP fail "Russo Trigger B", opening fade) on 1h, 15m
and 5m bars, on stocks that went up 100%+ in one day (INTRADAY_100: the high
reached 2x the prior close; CLOSE_100: the close did), 2025-10-01..2026-09-30.
The backtest calls `tranche-dashboard/engine.py` unchanged; the wrapper only
feeds bars in and reads trades out.

## Rule version

- GitHub `main` = `f7fad20` (2026-10-02 08:34 CT) when this was written.
- The line numbers in Dean's brief match commit `33a66ec` (2026-09-30), not
  `main`: opening_fade 100-115, vwap_fail 118-141, _ema_sleeve 589-605. On
  `main` they sit at engine.py:107-122, 125-148, 643-658.
- `33a66ec` → `main` in engine.py: only the per-symbol "close by end of
  day" option (default off, store.py:40, off in this backtest) and grade/note/
  tag recording. indicators.py and DEFAULT_SETTINGS (store.py:11-23) are
  identical. With that option off, signals and exits are the same.
- Step A on the PC: if local tranche-dashboard/ (engine.py, app.py,
  indicators.py, store.py) differs from origin/main in rule logic or default
  settings, STOP and list the differences.

## Decisions — first round (Dean, 2026-10-02)

1. **1R for EMA trades** = the engine's own `risk_dollars` sizing distance
   (75th-percentile adverse move of past crosses, floored at 1 ATR, else
   2 x ATR; engine.py:626-641, 679-681). R = P&L / risk_dollars as in
   engine.py:819-820.
2. **Shared VWAP slot:** run the engine as is (VWAP fail and opening fade share
   one position slot, engine.py:533, 539); split rows by the `trigger` tag
   (engine.py:548).
3. **VWAP 10-session time stop:** keep it on (`vwap_max_sessions`=10,
   store.py:21). It is the rule's own exit.
4. **VWAP scale-out:** one trade with combined R; both exit legs (time, price,
   size) on the row.
5. **Costs:** run at zero cost for gross R; live Excel formulas with the
   engine's defaults: 5 bps slippage per side, 10%/yr borrow / 360 per
   calendar night on the prior close (store.py:13-18, engine.py:730-740).
   Summary shows gross and net.
6. **Entry start:** INTRADAY_100 `added_at` = end of the signal bar (a
   15m/1h bar that closes after it, even if it contains it, may act).
   CLOSE_100: `added_at` = day 0 16:00, so a cross on day 0's last bar may
   fill at day 1's open (engine.py:388-392).
7. **Day-25 cutoff:** the engine's pause at the end of day 25
   (engine.py:665-667). Entry day = fill date; day-26 fills excluded.
8. **History window:** replicate the live 35-calendar-day window of 5-minute
   bars exactly (data.py:227-229).
9. **Pre-market:** included in the 2x search. The signal is the first bar,
   pre-market or regular, whose high reaches 2x the prior regular close.
   Price at signal = that bar's close, must be >= $1. The engine trades
   regular hours only; after a pre-market signal the first tradable bar is
   the first regular-hours bar that closes after it. HELD/FADED is judged on
   the regular-session close. First confirm FMP intraday includes
   pre-market; if not, STOP and tell Dean before choosing another source.
10. **Split inside a window:** back-adjust the whole window to one basis and
    keep the event; count how many events this applied to.
11. **Two events within 25 days:** count both, each in its own book; a Trades
    column shows when a trade also appears under another event; the summary
    reports how many trades are duplicated this way.
12. **Holding period:** trading days from entry to exit, same day = 0.

## Decisions — second round (Dean, 2026-10-02)

1. **Security master:** approved, up to 407 FMP profile calls, run on the PC.
   (See "Open for Dean": the final rules need 573.)
2. **2.5x suspect rule:** dropped entirely.
3. **Possible unapplied splits** are excluded as data errors and listed in
   the Excluded tab with the reason. Definition (unchanged from what Dean
   approved): a split recorded within ±5 calendar days, gold's split factor
   flat between the prior day and day 0, and a jump of 2.5x or more
   (`events.possible_unapplied_split`).
4. **Same bars under two tickers:** keep the ticker that was listed that day.
5. **As-traded** price and volume for the $1 and 10,000-share tests.
6. **No "3+ events" exclusion:** included; the Events tab flags symbols with
   3+ events in the list (`symbol_3plus_events`, `events_in_list`).
7. **IPOs with < 20 prior sessions:** average over the sessions available;
   flagged on the Events tab (`short_history`).
8. **Steps 3-5 run on Dean's PC** (local Claude Code with the FMP key), not in
   the cloud.

## Choices Dean approved as is

- Open trades are valued at the last regular-hours 5-minute close in the
  data and kept out of win %, average R and total R.
- Largest losing streak is counted in entry-time order within each
  strategy x timeframe x direction (and event type).
- A win is net R > 0 (the engine's own definition, engine.py:823).
- A pre-market signal sets `added_at` to 09:30, so yesterday's last bar
  cannot fill at today's open.
- The run charges borrow at 0.000001%/yr only to read the engine's own night
  count back for the workbook's borrow formula; gross P&L and R are unchanged.

## Facts established in the cloud session

- **Event source:** `core.ticker_history_daily_gold` (read-only),
  source `fmp_non_split_adjusted`, data through 2026-09-30. OHLC are
  split-adjusted (reconciled splits only), `raw_close` is as-traded, volume
  = raw / `cum_split_factor` (trading-system
  scripts/migrations/048_create_ticker_history_daily_gold.sql). The table
  is ~37M rows; the candidate query runs one month at a time
  (sql/candidates.sql); exports are in inputs/.
- **No exchange or security-type data** exists in any Stormport Supabase
  project (`core.ticker_metadata_cache.exchange` is null for 9,796 of 9,805
  rows), hence the FMP profile calls.
- **Gold data quality:** rows on non-trading days (INHD 2025-11-23,
  UOKA 2026-02-08), a ticker with trailing whitespace (`MGRT `), identical
  bars under two tickers (FMP ticker changes), unapplied splits.
- **Reference count:** Dean's quick count was 477 CLOSE_100 events across 356
  symbols with only the price and volume filters. Here: 507 / 365
  (adjusted basis), 489 / 363 (as-traded price and volume), 470 / 350
  (as-traded price, adjusted volume). The reference query is UNKNOWN; all
  are within 7%.
- **The cloud cannot call FMP:** the environment's network policy blocks
  financialmodelingprep.com, and no `FMP_API_KEY` is set there.

## Current counts (inputs/ as committed; no security master yet)

`python events.py`, as-traded basis, both suspect rules dropped:

| | INTRADAY_100 | CLOSE_100 |
|---|---|---|
| Candidates | 3,698 (969 symbols) | 2,083 (621) |
| Price < $1 (as-traded) | 2,633 | 1,501 |
| Avg volume < 10,000 (as-traded) | 100 | 80 |
| Data error: possible unapplied split | 74 | 58 |
| Duplicate whitespace row | 5 | 2 |
| Dated on a non-trading day | 4 | 2 |
| **PENDING** (exchange / type unknown) | **882 (540 symbols)** | **440 (330)** |

- Both lists share 329 pending symbol-days.
- Flags among pending: 301 INTRADAY / 76 CLOSE events belong to symbols with
  3+ events; 14 / 6 have short history; 27 / 12 have same-bars partners.
- HELD vs FADED (INTRADAY): 329 / 553.
- The INTRADAY $1 test here uses the as-traded price at the 2x level; the
  final test uses the signal bar's as-traded close.

## Open for Dean (raised 2026-10-02, not yet answered)

1. **Profile calls:** dropping the 2.5x rule raised the symbols needing a
   profile from 407 to **573** (same-bars partners included).
   `security_master.py --max-calls 407` stops without calling. Ask Dean
   before using more than 407.
2. **Unapplied-split exclusion is larger than approved:** 74 INTRADAY / 58
   CLOSE (33 / 49 pass price and volume) against the 27 / 41 Dean approved.
   - **Why it grew:** it now runs before the price and volume filters, and
     the suspect rules no longer remove events first. The definition is
     unchanged.
   - **Likely real moves among them:** 17 INTRADAY / 12 CLOSE have the
     split dated before the prior-close day. Several have jumps nowhere near
     the split ratio and may be real moves:
     - PLRZ 2025-12-02: 2.65x vs 1-for-6
     - FGL 2026-02-13: 2.62x vs 1-for-100
     - VCIG 2026-03-04: 2.91x vs 1-for-60
     - GSIW 2026-03-09: 3.87x vs 1-for-200
   - Every one is in the Excluded tab with the split date for review.
3. **Pull size:** upper bound before the exchange filter, from
   `python plan_pull.py`:
   - 1,322 events, 573 symbols, 67,120 symbol-sessions
   - 5.24M regular-hours bars (12.89M with pre/post-market)
   - about 14,214 requests at 7-day chunks
   - about 576 MB regular-hours only (1.42 GB with pre/post-market), at an
     estimated 110 bytes per bar
   - This must be re-estimated after Steps C and D, and needs Dean's
     approval before the pull.

## What's built and tested

All in research/tranche_event_backtest/. Tests use synthetic bars or a fake
FMP only; nothing has run on real intraday data.

| File | What it does |
|---|---|
| spec.py | Event definitions (2-day / 5-day variants defined, not run), filters, windows |
| sql/candidates.sql, sql/splits.sql | Read-only Supabase queries behind inputs/ |
| events.py | Both event lists with every decision above; writes out/events_*.csv, out/excluded_*.csv, out/reconciliation.csv |
| fmp.py | /stable-only FMP client: hard call budget, every call logged to cache/fmp_calls.jsonl, key never printed |
| security_master.py | Step C: one profile per pending symbol → inputs/security_master.csv; resumable; stops if over budget |
| probe.py | Step D: at most 3 calls (split basis, pre-market, time stamps, delisted, depth) → out/probe.json |
| plan_pull.py | Pull size (symbols, sessions, bars, requests, MB); uses probe.json when present |
| fetch_bars.py | Step E.1: resumable chunked 5-minute pull into cache/raw → cache/bars; `--extend` for open trades |
| intraday.py | Bar files, INTRADAY signal bar (pre-market included), one-basis split rescaling |
| replay.py | Feeds bars to engine.py on the dashboard's own check schedule (app.boundaries, Scheduler.retry_due) |
| trades.py | Reads engine trades back; scale-outs as one trade; net-R formula identical to the workbook's |
| runner.py | Every event x bar size as its own book, parallel, resumable (cache/results); duplicate flag |
| run_backtest.py | Step E.2-E.3: signals, basis, run, extensions, trades.csv, workbook, HTML, checks |
| report.py | Workbook: Assumptions, Trades, Summary x2, HELD vs FADED, By entry day, Events, Excluded, Checks; net R everywhere is a live formula |
| html_summary.py | One-page HTML: both summary tables + average net R by entry day per strategy (light/dark) |
| explain_trade.py | Hand-check helper: engine bar table, entry/exit log lines and raw bars for one trade |
| selftest.py | Synthetic checks (all pass, see below) |
| pipeline_test.py | Whole PC pipeline against a fake FMP on 3 real candidate events (passes) |

**Test results:**
- **Engine tests:** tranche-dashboard/test_tranche.py, 98 pass on `main`.
- **Replay R:** gross R per trade equals the engine's own tranche records
  exactly.
- **Cost formula:** the workbook's net-R formula equals the engine's own net
  R when the engine runs at 5 bps / 10%/yr. It is exact for one-exit trades;
  scale-outs differed by at most 0.0084R in the self-test, because the engine re-splits shares
  half/half when costs change equity.
- **Recalculation:** after LibreOffice recalculates the workbook, all 448
  synthetic trade formulas and every summary and entry-day row equal Python.
- **Speed on the 4-core cloud container** (synthetic, one event, ~86
  sessions): 1h about 6 s, 15m about 27 s, 5m about 107 s. That is
  ~140 s per event across the three books, about 13 h for 1,322 events on 4
  cores (fewer events after the exchange filter).

## What's left (Dean's PC prompt, Steps A-E)

- **A.** Report the local folder's state without touching it; put this
  branch in a separate worktree; compare the rules against origin/main.
- **B.** Confirm `FMP_API_KEY` and read-only Supabase access to
  `aqmifmnwzftpywhozhea`.
- **C.** Security master (budget question above), rebuild the events, report
  counts and reconciliation.
- **D.** Probe (at most 3 calls), re-estimate the pull, STOP for approval.
- **E.** Pull bars, run, extend open trades, workbook + HTML, hand-check 5
  trades, run the formula recalculation, push to this branch.

## Exact commands (run inside the worktree)

```
# one-time setup
git worktree add ../autoagent-pr5 claude/autoagent-backtest-report-lrmwqk
cd ../autoagent-pr5/research/tranche_event_backtest
pip install -r ../../tranche-dashboard/requirements.txt openpyxl
#   LibreOffice (soffice on PATH) is needed only for the workbook recalculation checks

# self-tests (no network, no FMP)
python ../../tranche-dashboard/test_tranche.py   # engine's own suite
python selftest.py                               # expect: SELFTEST PASS
python pipeline_test.py                          # expect: PIPELINE TEST PASS (fake FMP)

# rebuild the events (inputs/ as committed)
python events.py
#   to refresh inputs/ from Supabase first: run sql/candidates.sql per month
#   ({lookback}=1, {multiple}=2) into inputs/cand_YYYY-MM.csv and sql/splits.sql
#   ('2025-08-01'..'2026-11-30') into inputs/splits.csv; read-only

# Step C: security master (stops with a message if more than the budget is needed)
python security_master.py --max-calls 407
python events.py                                 # applies inputs/security_master.csv

# Step D: probe and estimate, then STOP for approval
python probe.py                                  # at most 3 FMP calls -> out/probe.json
python plan_pull.py TESTED                       # pull size for the final list

# Step E (only after Dean approves the pull)
python fetch_bars.py --max-calls <approved>      # resumable; re-run after an interruption
python run_backtest.py                           # resumable; prints the summary line when done
#   if it says trades are still open:
python fetch_bars.py --max-calls <approved> --extend
python run_backtest.py                           # repeat until it prints the summary line
#   hand checks: for each row of out/hand_check_picks.csv
python explain_trade.py <event_id> <timeframe> <entry_time>
#   write the working (bars, indicator values, fills, R) into out/hand_checks.md, then
python run_backtest.py --report-only --check-formulas
```

Outputs: out/tranche_event_backtest.xlsx, out/summary.html, out/trades.csv,
out/hand_checks.md. Commit out/ and inputs/security_master.csv; cache/ stays
local (gitignored).

## Caveats to keep in view

- **FMP exchange is current,** not as of the event date.
- **Security type** comes from isEtf / isFund and the company name
  (warrant / unit / right / preferred). security_master.py prints every
  non-common classification for review.
- **Basis from the probe:** if call 1 does not show a clear step or no step
  across the split, run_backtest.py stops. It also stops if out/probe.json
  is missing.
- **FMP's own split list** may differ from core.corporate_actions. If bars
  are FMP-adjusted, prior closes are carried with core.corporate_actions
  factors, and a mismatch shows up as "no 5-minute bar of day 0 reached 2x
  the prior close" in Excluded.
- **Chunk size:** 7 days per request is the dashboard's own choice
  (data.py:213-217). If call 1 returns fewer bars than a full 5-session
  request should, FMP is truncating; lower `plan_pull.CHUNK_DAYS` before
  pulling.
