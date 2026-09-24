# Tranche dashboard

A local web dashboard that watches the symbols you enter each day (or pre-market),
checks them after every hourly bar closes, and trades a **mock portfolio** in
three thirds per symbol. Performance is tracked in total, per tranche, per side
and per symbol.

**Mock portfolio only.** It never talks to a broker and never places an order.
Every fill is simulated.

```
pip install -r requirements.txt
python app.py                   # live prices (Yahoo), open http://127.0.0.1:8050
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
| **5/10 EMA** | 5 EMA crosses **below** 10 EMA → short. In *long & short* mode, 5 crosses **above** 10 → long | Opposite cross (it reverses in long & short mode), or the stop |
| **10/20 EMA** | Same rule using the 10 and 20 EMA | Opposite cross, or the stop |
| **VWAP** (always short) | **Russo "VWAP fail"** (Trigger B): an earlier hourly bar this session closed above VWAP, **and** this bar closes below VWAP, **and** this bar's high is below the session high so far. It fires only on the first bar where all three are true. There is no red-candle test, so a green bar can trigger it | Hourly close back above VWAP, flatten at the 16:00 close (setting), or the stop |

- **Setup choices, made per symbol:** *short only* or *long & short* for the two
  EMA tranches, plus the risk %. The VWAP tranche is short-only regardless.
- **Bars:** RTH-anchored hourly bars (9:30–10:30 … 15:30–16:00 ET), built from
  5-minute data. Session VWAP uses RTH typical price × volume. Pre- and post-market
  trading is ignored.
- **Timing:** each hourly close is evaluated `Check delay` minutes (default 2)
  after the bar closes. Entries and signal exits fill at that bar's close.
  **Nothing from before you added the symbol is traded**, so adding a name
  pre-market never back-fills old signals.
- **Stops:** each tranche has a hard stop at `Stop × hourly ATR(14)` from entry
  (default 1.5×). A stop is detected from each hourly bar's high or low and fills
  at the stop price, or at the bar's open if price gapped through it.
- **Sizing:** `shares = (equity × risk% ÷ 3) ÷ stop distance`, using the current
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
| `yahoo` (default) | none | Unofficial endpoint. It can rate-limit or change without notice |
| `alpaca` | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | The free plan's IEX feed carries only a small slice of total volume, so VWAP is approximate. Set `ALPACA_DATA_FEED=sip` if your plan includes consolidated data |
| `demo` | none | Synthetic regime-switching prices. **Demo P&L means nothing**: the generator trends, and trend-following does well on it |

## Files

- `engine.py` — strategy rules, sizing, fills, borrow fees and performance stats
- `indicators.py` — hourly resampling, EMA, ATR, session VWAP and crosses (pure functions)
- `data.py` — Yahoo, Alpaca and demo providers (5-minute bars)
- `store.py` — SQLite state (`tranche.db`), including settings defaults
- `app.py` — web server and the hourly scheduler
- `static/index.html` — the dashboard page
- `test_tranche.py` — offline tests

The server binds to `127.0.0.1` and has no authentication. Don't expose it to a network as-is.
