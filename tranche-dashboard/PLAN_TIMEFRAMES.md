# Plan: live IBKR trading on every timeframe, fast

Status: **Phase 1 built** (2026-10-23): fixes L1-L4 and L7, the timing
record and its dashboard display; its exit criteria need a market day on the
droplet. **Phase 2 built** (2026-10-24) except item 7 (thin-name bar
completeness, which needs Phase 1's arrival data); it ships off: nothing
changes until `IBKR_BOOKS` lists more than `5m`. Built with D2 (a) (an hourly
book has no 9:45 bar, so no state entry) and D6 (a) (one book per symbol);
D1 is unchanged (hourly bars 9:30-10:00, then on the hour) until checked.
Phase 3 items 4 (position guard) and 5 (link-down warning) built
2026-10-24; its other items wait on D3 and D4. **Phase 4 built** (2026-10-24,
waiting for Dean's yes before merge): D4 for the daily bar is a setting
(`daily_at_close`, default (a)); D7 waits on C4. Phases 5-6 not started.
Written 2026-10-22 from a read of the code at commit
7724ae7 and the measurements made on the droplet in October 2026.

## 1. Goal

- Every book can trade live at IBKR: **5-min, 15-min, hourly, and a new daily
  book**. You choose per symbol which book drives it, on the dashboard.
- Orders are fast after the bar forms:

| Book | Target: bar close to IBKR order |
|---|---|
| 5-min | 15-30 s |
| 15-min | 15-30 s |
| Hourly | 30-60 s |
| Daily | depends on decision D4 (section 4) |

- The Alpaca paper accounts keep mirroring each book, as today.
- **Gate:** no new book goes live at IBKR until the 5-min book has done one
  clean live round trip (one share in and out, clean log) on the faster
  pipeline from Phase 1.

## 2. What exists today (checked in the code)

| Area | Today | Where |
|---|---|---|
| Books | 1h, 15m, 5m. No daily book. | `app.py` `BOOKS` |
| IBKR link | Follows ONE book, set by `IBKR_BOOK` (1h, 15m or 5m; default 5m). Only that book's scheduler sends IBKR orders. | `ibkr.config_from_env`, `app.live_link`, `LiveSync.book_store` |
| Live button | Shown only on that book's tab; the API refuses a Live mark in any other book. | `static/index.html`, `app.py` `/symbols/<id>/live` |
| Live rule | 5/10 EMA short, 1 share: down-cross entry (stop = day's high), 9:45 state entry, 2 entries a day, cover on a 5-min close above the stop, up-cross, or 15:55. | `Engine._live_510`, `_live_stop`, `day_only` |
| Live stop | Checked only when the book processes its own bar. | `Engine._process_bar` calls `_live_stop` |
| 15:55 flat | Forced for every Live symbol, in every book. | `Engine.day_only` |
| Last bar of the day | Its signals execute at the next 9:30 open; a Live symbol takes no entry there. | `_process_symbol`, `_enter` |
| Overnight holds | Supported in the model (borrow accrual, next-open fills); blocked for Live symbols by the 15:55 rule. | `engine.py` |
| Data | FMP 5-min bars, about 35 days of history, shared between books by `CachedProvider`. Hourly and 15-min bars are built from the 5-min bars. | `data.py`, `indicators.resample` |
| Per-book settings | Check delay (seconds on the page, minutes in the DB), risk, etc. | `store.DEFAULT_SETTINGS` |

## 3. Speed: where the time goes today

### 3.1 The order of work at a bar check (verified)

1. The scheduler wakes within 1 s of `bar close + check delay`.
2. `Engine.tick` fetches and processes **every symbol of the book, one after
   another** (one FMP request each).
3. `run_sync` runs the **Alpaca paper sync first**, then the IBKR sync.
4. The IBKR sync reads the account and positions (cached by ib_async, fast).
5. For an opening short, `short_check` opens a market-data request and **waits
   up to 3 s** (`quote_wait_s`) for bid, ask and the shortable tick, then
   `submit` does the **same wait again** for its price check.
6. `submit` waits up to 5 s for IBKR to acknowledge (usually well under 1 s).

### 3.2 Defects found that cost time

| # | Defect | Effect | Evidence |
|---|---|---|---|
| L1 | `CachedProvider` reuses a fetch for 60 s within the same 5-min window. If the first fetch at a check misses the new bar, even by 2 s, every 5-second retry gets the same stale copy. | The bar is first seen about **70 s** after the close instead of about 12 s. | Reproduced in a simulation of the cache: the 11:55-12:00 bar, published at 12:00:12, was first returned at 12:01:10. |
| L2 | Alpaca paper sync runs before the IBKR sync. | Every Alpaca round trip delays the live order. | `Scheduler.run_sync` |
| L3 | Symbols are fetched one at a time. | About N x (one FMP round trip) before any order goes out. | `Engine.tick` |
| L4 | Two fresh market-data requests per opening short, each waiting up to 3 s. | Up to 6 s. | `IBKRBroker.quote`, `short_check`, `submit` |
| L5 | The live stop on a 15-min or hourly book is only checked at that book's bar close. | A stop cover reaches IBKR up to 10 min (15-min) or 55 min (hourly) late. | `_process_bar` |
| L6 | An hourly or 15-min bar counts as complete only when its last 5-min bar exists. A thin name with no trade in the last 5 minutes waits for a later bar or the 20-minute `STALE_WAIT`. | The hourly check for that name runs **5 to 20 minutes late**. | `Engine._load` |
| L7 | `orders.ts` is when the sync started, not when the order left. | Latency can't be measured from the log. On 2026-10-19 the PMI row at 14:17:00.36 is the sync start; the send time was not recorded. | `BrokerSync._submit` |

### 3.3 Budget after Phase 1 and Phase 2

| Step | Seconds | Status |
|---|---|---|
| FMP publishes the 5-min bar | 2 | Measured once (SPY, 2026-10-16 13:55 bar). Thin names: unknown, measured in Phase 1. |
| Check delay setting | 10 (5-min, 15-min); 10-20 (hourly) | Setting |
| Scheduler wake | up to 1 | Verified in code and tests |
| FMP fetch, all symbols in parallel | about 0.5-1.5 | Estimate; measured in Phase 1 |
| Engine compute | under 0.1 | Estimate |
| IBKR checks and send, with streaming quotes | under 1 | Estimate; measured in Phase 1 |
| **Bar close to order sent** | **about 12-14 (5-min, 15-min); 12-24 (hourly)** | Moderate confidence until Phase 1 data |
| IBKR fill of a marketable limit | about 1-2 more | Estimate; measured in Phase 1 |

Hourly and 15-min bars are built from the same 5-min bars, so they arrive as
fast as the 5-min bar that closes them.

## 4. Decisions needed from Dean

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Hourly bar alignment | (a) 9:30-10:00 first bar, then on the hour (today). (b) 9:30-10:30, 10:30-11:30, ... | Keep (a) **only if** your Finviz and TC2000 hourly charts show a 9:30-10:00 first bar. Checked in Phase 0. |
| D2 | Hourly state entry (the 9:45 rule has no 9:45 bar on an hourly chart) | (a) none on hourly. (b) on the 9:30-10:00 bar close. | (a), entries on crosses only. |
| D3 | Live hourly positions overnight | (a) same-day only, flat by 15:55. (b) hold overnight, flat by Friday 15:55. (c) hold over weekends too. | (b). Weekend holds depend on the Sunday 2FA re-login, and a missed approval leaves no stop management Monday morning. |
| D4 | When a signal on the last bar of the day trades (hourly 15:00-16:00, the daily bar) | (a) at 15:55 on the bar as it stands then (in regular hours). (b) market-on-close: decide before the exchange cutoff (NYSE 15:50, Nasdaq 15:55) and fill at the official close. (c) the next 9:30 open (today's rule). (d) after hours at 16:00:15-16:00:30. | (a). It is the only option that trades seconds after a signal in regular hours. (d) is not recommended: IBKR's acceptance of after-hours short sales is not confirmed, liquidity on these names after 16:00 is thin, and the adapter is regular-hours only by design. |
| D5 | Daily live rule | Entry, stop, exits and entries per day must be written down. A starting point: daily 5/10 down-cross judged per D4; stop = that day's high; cover on a 5-min close above the stop during regular hours, or a daily up-cross judged per D4; at most one entry a day. | Write it before Phase 5. |
| D6 | One symbol live in two books at once | (a) not allowed: one IBKR account holds one net position per symbol. (b) a second linked IBKR account, with its own $2,000 short minimum. | (a). |
| D7 | Daily book's Alpaca paper account | (a) a 4th paper account, if Alpaca allows it. (b) no paper link for the daily book. | (a) if Phase 0 shows it's possible, else (b). |
| D8 | Bar data for live symbols | (a) FMP for everything (today). (b) IBKR real-time bars for live symbols, FMP for history. | (a), and switch to (b) only if Phase 1 shows FMP misses the targets or revises bars too often. Phase 6 is that switch. |

## 5. Blockers and checks (Phase 0, no code)

| # | Check | How | Blocks |
|---|---|---|---|
| C1 | Droplet clock is synced | `timedatectl` must show `System clock synchronized: yes`. Latency is measured against this clock. | Phase 1 measurement |
| C2 | Hourly bar alignment on your charts | Claude in Chrome on Finviz Elite (and TC2000 if available): hourly chart of any stock, regular hours only, read the first two bar labels of a session. A third-party source says TC2000 uses 9:30-10:00 then on the hour; Finviz is unknown. | D1, Phase 2 |
| C3 | FMP daily endpoint and plan | One call on the droplet to `https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=SPY`, confirm about a year of bars and whether `from`/`to` are honoured; read the plan tier on the FMP dashboard. Published limits: Starter 300 calls/min and 20 GB per 30 days; Premium 750/min and 50 GB. | Phase 4 |
| C4 | 4th Alpaca paper account | Alpaca dashboard: try to create one. An Alpaca forum reply (not official) says 3 paper accounts per login, and another login breaks their terms. | D7 |
| C5 | A shortable symbol for the gate | `ibkr_check.py SYMBOL` all OK (easy to borrow, not closing-only). | The gate in section 1 |

**Hard blockers today: none for Phases 1 and 2.** Phase 3 needs D3 and D4.
Phase 4 needs C3 and D7. Phase 5 needs D4 and D5.

## 6. Phases

Each phase is one pull request with tests, docs (`IBKR.md`, this file) and a
deploy. Effort is my estimate of build and test time, moderate confidence.

### Phase 1: fast pipeline on the existing 5-min live path (about 1 day)

No new trading behaviour. Makes the 5-min live path meet 15-30 s and records
proof.

1. **Cache fix (L1).** `CachedProvider` never returns a cached fetch that lacks
   the newest bar the clock says should exist; it refetches instead. Cache hits
   are only for the same bar set.
2. **Live first (L2, L3).** At a check: fetch all symbols in parallel (thread
   pool, about 8 workers; well inside FMP's per-minute limits), process the
   book's Live symbols first, run the IBKR sync, then the remaining symbols,
   then the Alpaca paper sync in its own thread.
3. **Streaming quotes (L4).** Keep one IBKR market-data subscription (bid,
   ask, shortable tick 236) open per Live symbol. `short_check` and `submit`
   read it at once; the 3 s wait remains only as a fallback when no tick has
   arrived.
4. **Timing record (L7).** New columns and tables (section 7): bar end, send
   time, acknowledge time per order; one timing row per check; first-seen time
   of every symbol's bar.
5. **Dashboard.** "Recent IBKR orders" gets two columns: bar close to sent,
   bar close to filled. Each book shows its last check's timings.
6. **Exit criteria.** One market day of timing rows shows bar close to order
   sent under 20 s on the 5-min book, and `bar_arrivals` gives FMP's publish
   lag for the names you actually trade. Then the first live round trip (the
   gate).

### Phase 2: live on 15-min and hourly, same-day (about 1-1.5 days)

1. **`IBKR_BOOKS`** in `.env` (for example `5m,15m,1h`; default `5m`;
   `IBKR_BOOK` still accepted). A book not listed can't hold a Live mark.
2. **`LiveSync` reads every listed book.** Each Live symbol carries its book;
   `_net`, `_order_type`, `_renamed` and the reconciliation use that book's
   store.
3. **One book per symbol (D6).** A Live mark is refused while the symbol is
   Live in another book; the message names that book.
4. **Every listed book's scheduler runs the IBKR sync** after its own check.
   The sync lock, working-order check and the once-per-check refusal rule keep
   shared boundaries (12:00 closes a 5-min, 15-min and hourly bar) from sending
   twice. Covered by a test at a shared boundary.
5. **5-minute stop watch (L5).** A book with open Live tranches also wakes at
   every 5-min bar close (+ its check delay) and runs only the live stop rule.
   A stop cover goes out within the same 15-30 s as a 5-min book.
6. **Hourly state entry** per D2.
7. **Thin-name bar completeness (L6).** Designed from Phase 1's
   `bar_arrivals` data. The candidate: when a liquid reference symbol's 5-min
   bar for the same period has been published for N seconds and a watched
   symbol's has not, treat that symbol's 5-min bar as having no trades, so the
   hourly or 15-min bar is complete. N is chosen from the measured spread of
   publish times. If FMP's publish times vary too much, Phase 6 is the fix
   instead.
8. **Dashboard.** Live button on each listed book's tab; the dialog and the
   pill state that book's rules; the IBKR card already shows each symbol's
   book.
9. **Exit criteria.** Live round trips on a 15-min and an hourly symbol with
   order timings inside the targets.

### Phase 3: overnight live holds for the hourly book (about 1 day; needs D3, D4)

1. **Per-book setting** "Live: hold overnight" (off by default; the daily book
   is always on) and "flat by Friday 15:55" (on by default).
2. **Last bar** (15:00-16:00) per D4.
3. **Gaps.** The 5-minute stop watch runs from the 9:30-9:35 bar; a gap above
   the stop covers at the first 5-min close above it, at market.
4. **Unexpected position change.** If IBKR shows a different position in a
   Live symbol than the bot's last fill (a reverse split, a buy-in, a manual
   trade), the symbol's live link pauses and logs it instead of re-sending.
   Without this, a 1-for-10 reverse split of a 1-share short would leave the
   account flat while the model is short, and the bot would short again.
5. **Gateway banner.** The dashboard warns when the IBKR link is disconnected
   while a live position is open.
6. **Costs at 1 share.** IBKR charges borrow on positions held overnight.
   At 1 share of a $5 stock, 600% a year is about 8 cents a night. The daily
   loss limit still applies across the gap.

### Phase 4: daily book, model and paper (about 1-1.5 days; needs C3, D7)

**Built 2026-10-24.** C3 passed on the droplet (FMP's EOD endpoint answered for
SPY). As built, differences from the items below: no `daily_bars` table (FMP is
asked once per symbol per ET day and the answer kept in memory, so a restart
costs one request per symbol); D4 is the Daily-tab setting `daily_at_close`
((a) by default, (c) when off), and at (a) a cross is judged against the EMA
side read at the previous 15:55 check (`symbols.close_sides`), since the
official close can sit on the other side of the 15:55 price; `IBKR_BOOKS` does
**not** accept `1d` (live is Phase 5); a daily bar's next open is the next
daily bar's open, because the daily history reaches past the 5-minute bars.

1. **New book** `("1d", "Daily", "APCA_1D", "tranche_1d.db")`. `bar_bucket`
   and `session_bar_ends` get a whole-session case: today they would split a
   390-minute day into 9:30-13:00 and 13:00-16:00 (checked).
2. **Data.** About one year of daily bars from FMP's EOD endpoint, stored in a
   `daily_bars` table so a restart doesn't refetch; one request per symbol per
   day. Today's bar is built from the 5-min bars and replaced by FMP's official
   daily bar after the close.
3. **Checks** per D4 (15:55, and 9:30 for the next-open convention).
4. **Tranches.** 5/10 and 10/20 run as on other books. The VWAP-fail and
   opening-fade triggers can't fire on a one-bar session (both need an earlier
   bar of the same session), so the VWAP tranche is inert there.
5. **Paper link** per D7; without it the book runs unlinked, which the app
   already supports. The new key names join the redaction list
   (`data.SECRET_ENV`). `IBKR_BOOKS` accepts `1d` only in Phase 5.
6. **Dashboard.** The tab list, settings, reset and backups are built from
   `BOOKS`, so the daily tab appears with no page change beyond labels; the
   Trade Review page already iterates the books.

### Phase 5: daily live at IBKR (about 1 day; needs D4, D5)

1. The daily live rule from D5, on the Phase 2 multi-book link and Phase 3
   overnight handling.
2. If D4 is (b) market-on-close: the entry and up-cross exit go out as MOC
   orders before the exchange cutoff, read from IBKR's contract data
   (primaryExchange; PMI, for example, listed as AMEX). Stop covers stay
   market orders during the day.

### Phase 6: IBKR bars for live symbols (contingency, about 1.5 days)

Only if Phase 1 shows FMP's publish lag or revisions break the targets. IBKR
5-second real-time bars, built into 5-min bars for Live symbols; FMP keeps
supplying history. These are the bars your broker trades on.

## 7. Data changes

| Store | Table | Change | Phase |
|---|---|---|---|
| `tranche_live.db` | `orders` | add `book`, `bar_end`, `sent_at`, `acked_at` (`ts` stays the sync start; `filled_at` exists) | 1 |
| each book DB | `checks` (new) | `kind`, `bar_end`, `started_at`, `data_at`, `ibkr_start`, `ibkr_done`, `done_at`, `symbols`, `live`, `behind`; kept 30 days. Built in each book's database (not `tranche_live.db` as first planned) so books without the IBKR link are timed too | 1 |
| `tranche_live.db` | `bar_arrivals` (new) | `symbol`, `bar_end`, `first_seen_at`; primary key (`symbol`, `bar_end`); kept 30 days (the 5-min book's database when IBKR is not linked) | 1 |
| each book DB | `settings` | `live_hold_overnight` (0/1), `live_flat_friday` (0/1) | 3 |
| each book DB | `symbols` | none: `live` and `live_side` already exist | - |
| `tranche_1d.db` (new) | same schema as the other books, plus `daily_bars` (`symbol`, `day`, OHLCV, `source`, `fetched_at`) | | 4 |
| `.env` | `IBKR_BOOKS` (replaces `IBKR_BOOK`, which stays accepted); `APCA_1D_API_KEY_ID`, `APCA_1D_API_SECRET_KEY` (optional) | | 2, 4 |

FMP load after Phase 1, with one shared fetch per symbol per 5-minute check:
about 78 requests per symbol per day, so 10 symbols is about 780 a day.
The droplet's FMP usage log shows about 15-35 KB per request. Daily history adds one request per daily symbol per
day.

## 8. Risks

- **Bar revisions.** FMP changed the PMI 12:00-12:05 close from 14.30 (read at
  12:07) to 14.42 later that hour. The recorded-side rule takes such a cross
  one bar late instead of losing it; on the hourly book one bar late is an
  hour.
- **Feed differences.** 23 of 31 PMI closes that morning differed from Finviz
  by 1-14 cents. Crosses with a margin of a cent can land a bar apart from
  your chart. Phase 6 is the remedy if this matters in practice.
- **Weekend holds** depend on the Sunday 6 PM ET 2FA approval (D3).
- **More live books means more simultaneous orders** at shared boundaries;
  the sync lock serialises them and the tests in Phase 2 cover it.

## 9. Sources for outside facts

- FMP plan limits: https://site.financialmodelingprep.com/developer/docs/pricing/jesse and https://apis.io/rate-limits/financialmodelingprep/financialmodelingprep-rate-limits/
- FMP daily endpoint path: https://site.financialmodelingprep.com/how-to/how-to-get-data-to-build-market-charts-and-dashboards
- Alpaca paper accounts per login (forum, unofficial): https://forum.alpaca.markets/t/feature-request-more-paper-trading-accounts/18125
- IBKR market-on-close cutoffs: https://www1.interactivebrokers.com/en/trading/orders/moc.php
- Hourly bar alignment (third-party, TC2000): https://in.tradingview.com/scripts/tc2000/ and https://blog.tradervue.com/?p=745
