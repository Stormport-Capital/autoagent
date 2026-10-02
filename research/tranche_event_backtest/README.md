# Tranche-dashboard event backtest

A strategy comparison, not a model fit: the four autoagent tranche-dashboard
entries (5/10 EMA cross, 10/20 EMA cross, VWAP fail "Russo Trigger B", opening
fade) on 1h, 15m and 5m bars, run on stocks that went up 100%+ in one day. The
rules are never copied: the backtest calls `tranche-dashboard/engine.py` and
only feeds it bars and reads its trades back.

## Status (2026-10-02)

| Step | State |
|---|---|
| 1. Local checks | Done in the cloud against GitHub `main` `f7fad20`; local-PC items UNKNOWN (cloud) |
| 2. Event lists | Built from `core.ticker_history_daily_gold`; exchange / common-stock filter **pending a security master** |
| 3. Intraday data | **Not started**: waiting on Dean's approval of the pull size; FMP host is blocked in this cloud environment and no `FMP_API_KEY` is set |
| 4. Backtest | Waiting on Step 3 and Dean's confirmation that local rules = `main` |
| 5. Deliverables | Not started |

## Files

| File | What it does |
|---|---|
| `spec.py` | Event definitions (INTRADAY_100, CLOSE_100; 2-day/5-day variants defined, not run), filter thresholds, windows |
| `sql/candidates.sql` | Read-only monthly candidate query on `core.ticker_history_daily_gold` |
| `sql/splits.sql` | Read-only split list from `core.corporate_actions` |
| `inputs/` | The exports those queries produced for this run (candidates by month, splits, SPY session calendar) |
| `events.py` | Builds both event lists; every candidate lands in TESTED, PENDING, EXCLUDED (first failing reason) or SUSPECT (all reasons) |
| `plan_pull.py` | Sizes the 5-minute FMP pull (symbols, sessions, bars, requests) |
| `out/` | `events_*.csv`, `excluded_*.csv`, `reconciliation.csv` written by `events.py` |

## Re-running

```
python events.py       # event lists + reconciliation
python plan_pull.py    # pull size for PENDING + TESTED events
```

`EVENT_PRICE_BASIS` / `EVENT_VOLUME_BASIS` = `adjusted` (default, gold's own
convention) or `as_traded` switch the basis of the $1 and 10,000-share tests.

The inputs were exported through the Supabase connector one calendar month at a
time (a single pass over the year times out). To refresh them, run
`sql/candidates.sql` for each month with `{lookback}=1`, `{multiple}=2` and save
the `csv` column as `inputs/cand_YYYY-MM.csv`; run `sql/splits.sql` for
`2025-08-01`..`2026-11-30` into `inputs/splits.csv`. Other event definitions
(up 100% in 2 days, up 300% in 5 days) use the same query with a different
`{lookback}` / `{multiple}`.
