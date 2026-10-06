# Tranche dashboard

A local web dashboard that watches the symbols you enter each day (or pre-market),
checks them after every hourly bar closes, and trades a **mock portfolio** in
three thirds per symbol. Performance is tracked in total, per tranche, per side
and per symbol.

It runs **three books with identical rules**, an **Hourly** book, a **15-min**
book and a **5-min** book, side by side in one dashboard. Each has its own portfolio, settings and
performance, and optionally its own Alpaca paper account. Each book keeps its
own records: simulated fills, per-tranche attribution and borrow fees. It can also be **linked to an Alpaca paper account**. With that on,
after each hourly check the paper account is brought to the model's net
position with market orders. Alpaca is paper only: the paper endpoint is hard-coded.
**Step-by-step keys and setup: [SETUP.md](SETUP.md).**

**Interactive Brokers (paper or LIVE), one book, 5/10 short only:** set
`IBKR_ACCOUNT` and one IBKR account trades through IB Gateway. It follows the
**5-min book** (`IBKR_BOOK`) only. On that tab, a row's **Live** button sends
that symbol's **5/10 EMA tranche, short only**:
- 1 share; a long signal means flat;
- covered on a 5-minute close above the day's high (market order), or on the
  opposite cross;
- flat by the 3:55 PM close.

VWAP-fail and 10/20 can't trade live. Guards for real money:

- Share cap (default **1 share per symbol**, in the model's direction).
- **Daily loss limit** (default **$100**): flattens and halts for the day.
- Dashboard **kill switch** (Pause / Flatten & stop / Resume).
- Short-availability check before every new short.
- Per-order value cap.

A live account ID and `IBKR_MODE=live` must agree. Setup and details:
[IBKR.md](IBKR.md).

**How to use the dashboard:** click **Guide** in the dashboard header
(`static/guide.html`): every button, the daily routine, and how-tos.

**Always-on in the cloud:** see [CLOUD.md](CLOUD.md) (DigitalOcean +
Tailscale, password-protected, auto-restart, daily backups).

**Easiest on your own PC:** double-click `Start Dashboard.bat` (Windows) or
`Start Dashboard.command` (Mac). It installs what's needed, asks for your keys
on the first run, and opens the dashboard in your browser.

```
pip install -r requirements.txt
cp .env.example .env            # then paste your keys (see SETUP.md)
python app.py                   # uses Polygon when POLYGON_API_KEY is set; open http://127.0.0.1:8050
python app.py --provider yahoo  # no key: prices from Yahoo
python app.py --provider alpaca # Alpaca market data: set APCA_API_KEY_ID / APCA_API_SECRET_KEY
python app.py --demo            # synthetic prices on a fast simulated clock (no network)
python test_tranche.py          # offline test suite
```

Keep it running during market hours. After a restart it catches up on any hourly
bars that closed while it was down and fills them at those bars' closes.

## The three tranches

Every symbol gets a risk budget of **0.5–3% of equity**, chosen when you add it.
Each tranche gets one third of that budget.

| Tranche | Entry (hourly chart) | Exit |
|---|---|---|
| **5/10 EMA** | 5 EMA crosses **below** 10 EMA → short. In *long & short* mode, 5 crosses **above** 10 → long | **Only** the opposite cross (it reverses in long & short mode). No stop |
| **10/20 EMA** | Same rule using the 10 and 20 EMA | Only the opposite cross. No stop |
| **VWAP** (always short) | **Russo "VWAP fail"** (Trigger B): an earlier hourly bar this session closed above VWAP, **and** this bar closes below VWAP, **and** this bar's high is below the session high so far. It fires only on the first bar where all three are true. There is no red-candle test, so a green bar can trigger it. **Opening fade** (second entry path, for names that never trade above VWAP): bar 1 of the session is red and closes below VWAP, and bar 2 is red, closes below VWAP and has a lower high than bar 1; it fires at bar 2's close. Only bars 1-2 can trigger it. Same exits; tracked as its own row in Performance by tranche | **Russo exits**, stop checked first. **Stop:** the high of day at entry, never widened. From the next session on it trails down to the lowest full-session high since entry (Russo: trail it down as it rolls over). A bar whose high reaches it stops out at the stop price, or at the bar's open if it gaps through. **Targets:** half covers at the daily 10-day simple moving average of prior sessions' closes, the rest at the 20-day average, each filled when a bar's low touches it (setting: or all at the 10-day). **Time stop:** covers at the last check of the 10th session held, counting the entry day (setting; 0 = off). **Held overnight**, with no flatten at the close |

- **Setup choices, made per symbol:** *short only* or *long & short* for the two
  EMA tranches, plus the risk %. The VWAP tranche is short-only regardless.
- **Bars:** clock-aligned hourly bars, the same as standard hourly charts:
  9:30–10:00 (a half hour), then 10–11 … 15–16 ET. Built from
  5-minute data. Session VWAP uses RTH typical price × volume. Pre- and post-market
  trading is ignored.
- **Timing:** each hourly close is evaluated `Check delay` minutes (default 2)
  after the bar closes. An hour is only treated as complete once the feed has
  printed past its end, or 20 minutes have passed. So a 15-minute-delayed feed
  just waits; it never trades on a partial hour. Checks run at 10:00 … 15:00,
  and entries and signal exits fill at that bar's close. The 15:00–16:00 bar is
  acted on at the **next day's 9:30 open check**, filled at the opening price.
  A signal only trades if it executes after you added the symbol. So a name
  added pre-market can act on yesterday's last bar at the open, but never on
  anything older.
- **EMA tranches have no stop.** They enter on a cross and exit only on the
  opposite cross, at that bar's close (or at the next open for the day's last
  bar), whatever the loss. There is no ATR stop and no gap exit.
- **What counts as a cross:** EMAs are compared at the price a chart shows:
  cents at $1 and up, 4 decimals below $1. **Equal EMAs are never a signal.**
  A cross fires on the first bar where one line is clearly on the other side,
  and the last bar where they differed had it on the opposite side. Touching and
  bouncing back the same way is not a cross.
- **VWAP signals never carry overnight.** VWAP resets each session. A VWAP fail
  on the day's last bar is logged and skipped, not traded at the next open.
  The VWAP tranche can only enter intraday, after an earlier bar of *that day*
  closed above VWAP.
- **VWAP tranche notes (as in the Russo engine):** if a bar reaches the stop
  and touches the 10-day average, it counts as a stop, not a win. There are **no
  setup or entry filters**: you choose the symbols. Russo's +100%-in-5-sessions
  filter is not applied. So a VWAP fail that fires while price is already at or
  below the 10-day average is still shorted, and it covers at the next bar's
  open. That's roughly a scratch trade that costs slippage. A bar that gaps
  below the target covers at its open. The half exit at the 20-day average
  only applies when the 20-day is below the 10-day and the position has at
  least 2 shares; otherwise everything covers at the 10-day. The 10-day target
  needs 10 prior sessions of data and the 20-day needs 20; with less history
  that target doesn't exist yet. The time stop covers at the last check of the
  Nth session (15:00 hourly, 15:45 15-min, 15:55 5-min), at that bar's close.
- **Sizing:** `shares = (equity × risk% ÷ 3) ÷ risk per share`. For the VWAP
  tranche, risk per share is the distance from entry up to the session high,
  its real stop. The EMA tranches have no stop, so risk per share
  is worked out from the stock's own history. Take every past cross-to-cross
  trade of the same EMA pair in the loaded data (about 35 sessions). Measure
  how far price moved against it, close to close, before the opposite cross,
  and use the **75th percentile** of those moves. It's a % of price, floored at
  1 ATR, and falls back to `2 × ATR` with fewer than 5 past crosses. So a
  losing EMA trade typically costs about the risk %; about 1 in 4 historically
  cost more. It only sets the size, never an exit.
- **Grades:** pick a grade when adding a symbol, or change it later from the
  watchlist row. A+ = 3%, A = 2%, B = 1.5%, C = 1% of equity for the symbol,
  split across its three tranches. "Custom" uses the slider (0.5–3%). A grade
  change applies to new entries only. Sizing uses the current
  mark-to-market equity. Gross exposure is capped at `Max gross leverage × equity`
  (default 2×). An entry that works out to less than one share is skipped and
  logged.
- **Borrow:** every short is assumed borrowable (the model does not check
  availability). The fee is the symbol's own rate (Borrow button on its row, e.g.
  the IBKR fee from iBorrowDesk) or the book default, **250%/yr** (configurable;
  small caps that just ran were 46-634%/yr at IBKR in Oct 2026). It is charged per
  calendar night held (Friday → Monday is 3 nights) on the prior close's notional
  ÷ 360 (or 365). A short covered the same day pays no borrow, as at IBKR for
  ordinary shorts opened and covered the same trading day.
- **Overnight borrow limit (default 200%/yr):** a symbol whose own entered rate is
  above it has its shorts closed at the 15:55 close and takes no new shorts from
  15:55, like a close-by-end-of-day name. Symbols on the default rate are not
  forced flat. 0 turns it off.
- **Costs:** 5 bps slippage on each side by default. No commissions.
- **Controls:** pause a symbol (open tranches are still managed, but no new
  entries), close by end of day (per symbol: trades all session, no new entries
  from 15:55 ET, flat at the 15:55 5-minute close, nothing overnight; every book
  gets an extra 15:55 check for it), flatten a symbol, remove it (flattens first; its closed trades stay
  in the history), rename it after a ticker change (open tranches move to the new
  ticker, closed trades keep the old one, optional reverse-split ratio), or reset
  the whole portfolio (the only action that deletes history, after a backup).

## Trade Review page

The **Trade Review** button (next to the book tabs) opens `/review`: every closed
trade from all three books, with filters (book, dates, strategy, side, symbol,
grade, exit type, sector, tag), KPI tiles (net P&L, win rate, profit factor,
expectancy in $ and R, payoff, best/worst, drawdown, hold), a cumulative P&L
curve, a P&L calendar with per-day review notes, an R-multiple histogram,
breakdowns by strategy / book / symbol / side / grade / exit type / time of day /
weekday / hold time / price / sector / market cap / float / tag, rule-based
findings, and a sortable trade list where each trade takes a note and tags. CSV
download of the filtered trades.

It is read-only over the model: notes and tags live in each book's `tranches`
table, day notes and the FMP company-profile cache in the hourly book's
database (`day_notes`, `profiles`). Grade is recorded on each trade at entry.
Sector, market cap and float are FMP's current values (not point-in-time) and
need `FMP_API_KEY`. Code: `review.py`, `static/review.html`.

## Exporting trades (TradesViz)

On the **Closed trades** tab, pick a date range and click **Download CSV**. The
file is for the book you're viewing, with two rows per trade: the opening and
the closing execution. Columns: `Date, Time` (ET), `Symbol`, `Action`
(BUY/SELL; a short opens with SELL and closes with BUY), `Direction`
(Long/Short), `Type` (Open/Close), `Quantity`, `Price` (the model's fill,
slippage included), `Fees` (borrow, on the closing row), `Tranche`, `TradeID`.
A trade is included when it was *opened* in the range, so both legs always
come together. Tick "include open positions" to add entries that haven't
closed yet. Direct link: `/api/<1h|15m|5m>/export/tradesviz.csv?from=YYYY-MM-DD&to=YYYY-MM-DD`.

## Data providers

| Provider | Key | Caveat |
|---|---|---|
| `polygon` (recommended) | `POLYGON_API_KEY` | Consolidated all-exchange volume, so VWAP is accurate. Plans without real-time data are 15 minutes delayed, and checks then land about 15 minutes after each hour |
| `yahoo` (fallback without a Polygon key) | none | Unofficial endpoint. It can rate-limit or change without notice |
| `alpaca` | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | The free plan's IEX feed carries only a small slice of total volume, so VWAP is approximate. Set `ALPACA_DATA_FEED=sip` if your plan includes consolidated data |
| `demo` | none | Synthetic regime-switching prices. **Demo P&L means nothing**: the generator trends, and trend-following does well on it |

## Files

- `engine.py` — strategy rules, sizing, fills, borrow fees and performance stats
- `indicators.py` — hourly resampling, EMA, ATR, session VWAP and crosses (pure functions)
- `data.py` — Polygon, Yahoo, Alpaca and demo providers (5-minute bars)
- `broker.py` — broker sync: net-position mirror, order log, reconciliation, share cap,
  daily loss limit and kill switch; the Alpaca paper client
- `ibkr.py` — Interactive Brokers client (ib_async via IB Gateway), paper or live;
  its orders, log, kill switch and loss latch are kept in `tranche_live.db`
- `deploy/ib-gateway/compose.yml` — IB Gateway + IBC in Docker for the server
- `store.py` — SQLite state (`tranche.db`), including settings defaults
- `app.py` — web server and the hourly scheduler
- `static/index.html` — the dashboard page
- `review.py`, `static/review.html` — the Trade Review page (stats, calendar, journal)
- `static/guide.html` — the in-app Guide
- `test_tranche.py` — offline tests

Locally the server binds to `127.0.0.1`. Set `TRANCHE_PASSWORD` in `.env` to
require a password (HTTP Basic auth, any user name). The cloud setup does this
and listens only on the private Tailscale address.
