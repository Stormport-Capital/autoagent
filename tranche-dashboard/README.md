# Tranche dashboard

A local web dashboard that watches the symbols you enter each day (or pre-market),
checks them after every hourly bar closes, and trades a **mock portfolio** in
three thirds per symbol. Performance is tracked in total, per tranche, per side
and per symbol.

It runs **two books with identical rules**, an **Hourly** book and a **15-min**
book, side by side in one dashboard. Each has its own portfolio, settings and
performance, and optionally its own Alpaca paper account. Each book keeps its
own records: simulated fills, per-tranche attribution and borrow fees. It can also be **linked to an Alpaca paper account**. With that on,
after each hourly check the paper account is brought to the model's net
position with market orders. It is paper only: the paper endpoint is hard-coded,
and there is no live-trading setting. **Step-by-step keys and setup:
[SETUP.md](SETUP.md).**

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
| **VWAP** (always short) | **Russo "VWAP fail"** (Trigger B): an earlier hourly bar this session closed above VWAP, **and** this bar closes below VWAP, **and** this bar's high is below the session high so far. It fires only on the first bar where all three are true. There is no red-candle test, so a green bar can trigger it | **Russo exits**, stop checked first. **Stop:** the first later bar whose high reaches `max(high of day including that bar, entry)`, meaning a new high of day. On a later session that also requires trading back above entry. It fills at that level, the worst price in the bar. **Target:** the daily 10-day simple moving average of prior sessions' closes, filled at the target when an hourly low touches it. **Held overnight**, with no flatten at the close |

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
- **VWAP tranche notes (as in the Russo engine):** if a bar makes a new high of
  day and touches the 10-day average, it counts as a stop, not a win. There are **no
  setup or entry filters**: you choose the symbols. Russo's +100%-in-5-sessions
  filter is not applied. So a VWAP fail that fires while price is already at or
  below the 10-day average is still shorted, and it covers at the next bar's
  open. That's roughly a scratch trade that costs slippage. A bar that gaps
  below the target covers at its open. The 20-day average second target is
  not modelled: it only matters with scale-ins, and those are off in the Russo
  harness too. The target needs 10 prior sessions of data; with less history
  there is no target until there is.
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
- **Borrow:** every short is assumed borrowable. Hard-to-borrow cost is
  **10%/yr** (configurable), charged per calendar night held (so Friday → Monday
  is 3 nights) on the prior close's notional ÷ 360 (or 365). A short covered the
  same day pays no borrow.
- **Costs:** 5 bps slippage on each side by default. No commissions.
- **Controls:** pause a symbol (open tranches are still managed, but no new
  entries), flatten a symbol, remove it (flattens first), or reset the whole
  portfolio.

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
- `broker.py` — Alpaca paper link: net-position sync, order log, reconciliation
- `store.py` — SQLite state (`tranche.db`), including settings defaults
- `app.py` — web server and the hourly scheduler
- `static/index.html` — the dashboard page
- `test_tranche.py` — offline tests

Locally the server binds to `127.0.0.1`. Set `TRANCHE_PASSWORD` in `.env` to
require a password (HTTP Basic auth, any user name). The cloud setup does this
and listens only on the private Tailscale address.
