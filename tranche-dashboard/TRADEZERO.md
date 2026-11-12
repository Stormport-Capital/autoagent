# TradeZero link (paper or LIVE)

One TradeZero account, paper or live, trades the books listed in `TZ_BOOKS`
(the **5-minute book** by default, or any of `5m`, `15m`, `1h`) over
TradeZero's REST Trading API. It is a second live venue next to Interactive
Brokers (`IBKR.md`); **one of the two is linked at a time** (`LIVE_BROKER`).
Everything in `IBKR.md` about *what* trades live applies unchanged: the Live
button on the listed books' tabs (a symbol may be Live in several books; the
account holds the sum of their positions), the 5/10 EMA tranche only, short
only, the share cap per book, the fixed entry stop checked at every 5-minute
close, flat by 15:55 when the row's EOD close is on (off = may hold
overnight), at most two entries a day, the daily loss limit and the
kill switch. This file covers only what is different at TradeZero.

Why TradeZero: IBKR refused most of the names tried in October and November
2026 for borrow ("locate needed", "not shortable", closing-only, a small-cap
compliance flag). TradeZero sells locates per share through the same API, so a
name IBKR will not lend can still be shorted, at a known price, within caps.

## What is different from IBKR

| | IBKR | TradeZero |
|---|---|---|
| Connection | IB Gateway in Docker, 2FA, weekly cold restart | HTTPS calls to `webapi.tradezero.com` with a key pair; nothing to run or log in to |
| Paper vs live | account ID `DU…` / `U…`, `IBKR_MODE` | same host for both; the **key pair** decides. The dashboard checks TradeZero's `accountType` ("Paper" or not) against `TZ_MODE` on every call path and sends nothing on a mismatch |
| Quotes | bid/ask/last from IBKR; marketable limit orders | **the API has no market data.** Orders are **market** by default (`TZ_ORDER_TYPE=market`), regular hours only, which is where every order of this link falls. The order-value cap uses the model's last price |
| Borrow | tick 236; a refused short is held for the day | `is-easy-to-borrow` before every entry. Paper: not easy to borrow = skipped (paper has no locates). Live: a **locate** is bought when its fee is within `TZ_LOCATE_MAX_PCT` percent of the share price and the day's spend stays within `TZ_LOCATE_MAX_USD_DAY`; defaults off (0%) / $5 |
| Order id | IBKR's order id | the dashboard's own `clientOrderId` (`td-SYMBOL-…`), never reused: TradeZero refuses a repeat forever |
| Route | SMART | paper routes itself; live needs a route name the account offers: `TZ_ROUTE`, else `SMART`, else the only route offered |
| Rate limits | n/a | one call at a time, spaced to TradeZero's per-endpoint limits; a 429 waits 2 s, then 4 s, then gives up until the next check |

### Orders, exactly

- **Entry** (the model's 5/10 down-cross or the 9:45 state entry): `Sell`,
  `openClose: Open`, `Market`, `Day`, 1 share. Refused before it is sent when
  the model has no last price for the symbol (the value cap needs one), when
  the account is not `Active`, when the position would pass `TZ_MAX_SHARES`,
  or when a buy would open a long.
- **Cover** (stop, up-cross, 15:55 when EOD close is on, Live off, kill switch, loss limit):
  `Buy`, `openClose: Close`, `Market`. A cover is never refused for lack of a
  price: leaving a short open is the bigger risk at one share.
- `TZ_ORDER_TYPE=limit` sends `Limit` at the model's last price through the
  market by `TZ_LIMIT_COLLAR_PCT` (3% default). BrokerSync cancels one still
  unfilled after `TZ_REPRICE_S` and re-prices it on the next follow-up sync.
  The stop cover is a market order whatever this setting says. A cover with
  no model price falls back to market.
- A rejection comes back as HTTP 200 with `orderStatus: Rejected` and a
  reason in `text` (`R62`, `R95`, `R100`…). It is recorded like an IBKR
  rejection: a borrow refusal holds new shorts in that symbol for the day,
  anything else is retried once per bar check.
- After a send, the order is read back every half second for up to five
  seconds (TradeZero can answer 404 for a moment after a POST). An order the
  API no longer knows, from an earlier session, is recorded as expired.
- A `Rejected` order is never cancelled (TradeZero would overwrite the
  rejection reason with `R130`).

### Locates (live only)

1. `GET is-easy-to-borrow` → true: the short goes out. No answer: it goes out
   and TradeZero's own check decides.
2. false → the account's locate **inventory** is read; enough located shares
   already there: the short goes out.
3. Otherwise, if `TZ_LOCATE_MAX_PCT` is above 0 and the model has a last
   price for the symbol (no price = no locate): a **quote** for 100
   shares (the API minimum; lots of 100) is requested. TradeZero answers
   through its locate history within about 2–10 s with a per-share price, or
   a reason ("No shares available"). The cheaper of a pre-borrow / single-use
   pair is taken.
4. The quote is **accepted** only when the fee per share is at most
   `TZ_LOCATE_MAX_PCT` percent of the share price (the model's last price;
   0.5 means half a percent: 5 cents on a $10 stock, $1 on a $200 stock) and
   today's locate spend (read from TradeZero's own history, so it survives a
   restart) + this one ≤ `TZ_LOCATE_MAX_USD_DAY`. Otherwise the short is
   skipped and the dashboard log says the price and the percent.
5. Every bought locate is in the live log: "bought a locate for 100 SYM at
   $0.0100/share (0.20% of the $5.00 price; $1.00); today's locate spend
   $1.00".
6. One quote per symbol per 30 s (TradeZero's rule); a second one within that
   window waits for the next check.

A single-use locate covers one short; a standard locate or pre-borrow returns
to the pool on the cover, so a second entry in the day may not need another.
Locate fees are charged whether or not the short fills.

## Set up

### 1. TradeZero account (once)

- A **TradeZero America** margin account (TradeZero Europe accounts are not
  the same product). Shorting needs margin, and US regulation needs $2,000 or
  more in equity to short; TradeZero does not count day trades toward the
  pattern-day-trader rule on accounts over $25,000 only - check the current
  rule with TradeZero before funding.
- Enable **API Trading** in the TradeZero account (the add-on on the
  Subscriptions page) and sign the data and API agreements it asks for. The
  market-data agreements do not matter to the dashboard: it reads no market
  data from TradeZero.
- On the **API Keys** page, generate a **paper** key pair first. The key ID
  and secret are shown once. Type them into the server's `.env` only
  (below); never paste them anywhere else, never into this chat.
- Note the **account ID** the API reports (`GET /accounts` → `account`,
  for example `TZP…` for paper). `tz_check.py` prints what the keys see.

### 2. The dashboard

Add to `/opt/tranche/autoagent/tranche-dashboard/.env` (on the server, with
`nano`; the values are typed there and nowhere else):

```
TZ_ACCOUNT=TZP12345678
TZ_MODE=paper
TZ_API_KEY_ID=…
TZ_API_SECRET_KEY=…
TZ_BOOKS=5m,15m,1h
```

If `IBKR_ACCOUNT` is still in `.env`, add `LIVE_BROKER=tradezero` as well:
with both accounts present and no choice, the dashboard links **neither** and
says so at startup. Then:

```bash
systemctl restart tranche
journalctl -u tranche -n 40 --no-pager
```

The startup lines should show:

```
TradeZero paper: OK - TZP12345678 net liquidation $…; cap 1 share(s) per symbol, daily loss limit $100; 5-min, 15-min, Hourly books, 5/10 EMA short only, flat by 15:55; 0 live symbol(s)
```

The broker card on every tab is now **TradeZero paper account**; the Live
buttons, pills and log say TradeZero. Your Live selections carry over from
the IBKR link (they live in the books, not in the broker).

### 3. Check a symbol (read-only)

```bash
cd /opt/tranche/autoagent/tranche-dashboard
sudo -u tranche /opt/tranche/venv/bin/python -I tz_check.py CLRO
```

Prints OK/FAIL for the keys, the account type against `TZ_MODE`, the account
status, equity, positions, the route (live), whether the symbol is easy to
borrow, located shares, and a `VERDICT` line. On a live account,
`--locate-quote` also asks TradeZero the locate price for 100 shares and
prints it; nothing is accepted or bought.

### 4. Paper test, then live

1. **Paper** for at least a day: positions match the model's direction at one
   share, a rejection is recorded once and not re-sent, Flatten & stop then
   Resume works, the 15:55 flat goes out. Paper lists every symbol as easy
   to borrow, so paper proves the order path, not the borrow.
2. **Live:** generate a **live** key pair on the API Keys page, replace the
   two keys and `TZ_ACCOUNT` in `.env`, set `TZ_MODE=live`, and decide the
   locate caps (`TZ_LOCATE_MAX_PCT`, `TZ_LOCATE_MAX_USD_DAY`). Restart.
   Switching orders on for a LIVE account requires typing **LIVE** on the
   dashboard. The link refuses to send anything if TradeZero reports the
   account as Paper while `TZ_MODE=live`, or the other way round.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `TZ_ACCOUNT` | (off) | The account ID TradeZero's API reports for the key pair (`tz_check.py` prints it) |
| `TZ_API_KEY_ID` / `TZ_API_SECRET_KEY` | (required with `TZ_ACCOUNT`) | The key pair from TradeZero's API Keys page; paper or live pair |
| `TZ_MODE` | `paper` | `paper` needs an account TradeZero reports as `Paper`; `live` needs any other type |
| `TZ_BOOKS` | `5m` | Books whose Live symbols trade here, from `5m`, `15m`, `1h`. The daily book is refused, as at IBKR |
| `LIVE_BROKER` | (auto) | `ibkr` or `tradezero`; required when both `IBKR_ACCOUNT` and `TZ_ACCOUNT` are set |
| `TZ_SHARES` | (off) | A fixed position size: every TradeZero short is exactly this many shares whenever the model's tranche is short, whatever the model's own size. `TZ_SHARES=100` matches the 100-share locate minimum. Sets `TZ_MAX_SHARES` to the same number unless that is set higher |
| `TZ_MAX_SHARES` | 1 | Without `TZ_SHARES`: the model's size, capped here; short only |
| `TZ_DAILY_LOSS_LIMIT` | 100 | $ loss in a day that halts and flattens (live: must be above 0) |
| `TZ_MAX_ORDER_USD` | 1000 | Max value of one order, at the model's last price. With `TZ_SHARES=100` this must cover 100 × the share price, or the entry is refused |
| `TZ_ORDER_TYPE` | `market` | `market`, or `limit` at the model's last price with a collar |
| `TZ_LIMIT_COLLAR_PCT` | 3 | How far through the model's price a limit goes |
| `TZ_REPRICE_S` | 45 | Cancel and re-price an unfilled limit after this many seconds |
| `TZ_ROUTE` | (SMART) | Live only: a route name from the account's route list |
| `TZ_LOCATE_MAX_PCT` | 0 | Live only: the most to pay for a locate, as a percent of the share price (0.5 = half a percent per share: 5 cents on a $10 stock, $1 on a $200 stock); 0 = never buy one. The model's last price is the reference; no price, no locate. The old `TZ_LOCATE_MAX_PER_SHARE` (cents) is refused, not ignored |
| `TZ_LOCATE_MAX_USD_DAY` | 5 | Live only: the most to spend on locates in a day |

## Not covered

- **No quotes from TradeZero.** The dashboard reads prices from its data
  provider (FMP / Polygon) as before; TradeZero only receives orders. A
  market order's fill price is known only after the fill.
- **No outside-hours orders.** TradeZero rejects market orders outside
  9:30–16:00 ET (`R100` live, `R78` paper); a flatten after 16:00 waits for
  the next open, as at IBKR.
- **No resting stop orders**, as at IBKR: stops act at the 5-minute checks.
- **Costs are not estimated here.** Commission, regulatory fees, locate fees
  and overnight borrow are on TradeZero's own pricing page for your plan;
  check them before the first live share.
- **The locate flow is built from TradeZero's documentation and tested
  against a fake of it**, not yet against a live account. The first live
  locate is a test: watch the log line and TradeZero's own locate history.
- **Symbol format.** The dashboard sends its own tickers unchanged
  (uppercase). Class shares (`BRK.B`) have not been checked against
  TradeZero's spelling.
