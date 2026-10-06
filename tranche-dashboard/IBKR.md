# Interactive Brokers link (paper or LIVE)

One Interactive Brokers account, paper or live, trades **one book**:
`IBKR_BOOK`, the **5-minute book** by default. The scope is fixed in code:

| Rule | Where it is enforced |
|---|---|
| Only the 5-min book's symbols can be Live; the Live button exists only on that tab, and the server refuses it elsewhere | `app.py` live route, `LiveSync.selections` |
| Only the **5/10 EMA tranche** can be Live. VWAP-fail and 10/20 are refused | `Engine.set_live` (`LIVE_SLEEVES`), `LiveSync.selections` |
| **Short only.** The account never goes above 0 shares; a long signal in the model means flat | `LiveSync.desired`; the adapter refuses any buy that would open a long |
| **1 share** (`IBKR_MAX_SHARES`, the only size setting) | `BrokerSync.desired` and the adapter |
| **Flat by 15:55**, whatever the symbol's own EOD switch | `Engine.day_only`: a Live symbol is close-by-end-of-day in the model |
| **No new entry from 15:55**, and no entry at the next open from the 15:55-16:00 bar | `Engine._enter` |
| **Entries:** the 9:45 state entry or a later down-cross; none before 9:45; at most 2 a day | `Engine._live_510` |
| **Hard stop, fixed at entry:** cover on a 5-minute close above it, as a **market** order | `Engine._live_stop`, `LiveSync._order_type` |
| **$100 daily loss limit** | `BrokerSync._check_loss` |

The Alpaca paper accounts are not affected; each book keeps its own.

## The live 5/10 short, exactly

All bars are regular-hours 5-minute bars (9:30-16:00 ET). The EMAs are the
5- and 10-period EMAs of those closes, continuous across days (no reset at the
open). EMAs are compared at the chart's price tick (cents from $1, 4 decimals
below), and equal is neither above nor below.

**Entries** (short only, tagged in `tranches.trigger`):
1. **`state_0945`:** on the 9:40-9:45 bar's close, if the 5 EMA is below the
   10 EMA. No cross is needed.
   - **Stop:** the opening-range high, meaning the highest high of the
     9:30-9:45 bars.
   - Checked at 9:47; the IBKR order goes out then.
2. **`cross`:** on a later bar whose close puts the 5 EMA below the 10 EMA,
   where the last earlier bar on which they differed had the 5 above.
   - **Stop:** the day's high at that moment, entry bar included.
- No short entry on bars ending before 9:45.
- At most **2 entries a day** per symbol: the 9:45 entry plus one cross, or
  two crosses. After a stop-out, only a fresh down-cross re-enters.
- No entry from 15:55, and none at the next open from the 15:55-16:00 bar.
- Size in the model: the existing EMA sizing unit. Live: 1 share
  (`IBKR_MAX_SHARES`).
- The stop is stored in `stop_price`. The per-share distance from entry to the
  stop is stored in `stop_dist`, so R can be computed from it:
  `(gross_pnl - borrow_fees) / (qty * stop_dist)`.
- The model enters at the bar close (less 5 bps). IBKR gets a sell for 1
  share, as a marketable limit order, right after that bar's check. Skipped
  when:
  - the symbol is paused;
  - IBKR shows no shares to borrow;
  - IBKR refused a short in that symbol that day;
  - the kill switch or the loss limit is on.

**Exits**, whichever comes first:
1. **Stop:** a 5-minute bar after entry closes **above `stop_price`**.
   - The level is fixed at entry and never moves, even if a later wick sets
     a new high.
   - A close equal to the stop is not a stop.
   - The cover is a **market** order.
2. **Up-cross:** a 5-minute close puts the 5 EMA above the 10 EMA. The cover
   is a marketable limit order, and the account never goes long.
3. **15:55:** flat at the close of the 15:50-15:55 bar (marketable limit).
- Kill switch, loss limit, Live off and Remove also cover.

## Choosing what trades live

- On the **5-min** tab, click **Live: off** on a row, tick **5/10 EMA cross
  (short)**, then click **Save**. The row shows `IBKR 5/10 short · flat 3:55`
  (`LIVE …` once the account is live).
- Turning Live on applies these entry and stop rules to the model's 5/10
  tranche for that symbol, and makes the symbol flat by 15:55 **in the
  model**, for all its tranches. The model's other tranches keep trading, but
  nothing else is sent to IBKR.
- **Live off, Remove, or a book Reset** closes that ticker's IBKR position at
  the next sync. A ticker the link ever traded is never left behind.

## What it does

- **It follows the direction, not the size.** The account holds **−1 or 0
  shares** of each Live symbol: −1 while the model's 5/10 tranche is short,
  0 otherwise. So the live P&L is **not** the model's P&L. It tests the
  plumbing: signals, orders, borrow, fills and fees.
- **When orders go out:** after each 5-min book check, every 5 minutes.
- **Daily loss limit** (`IBKR_DAILY_LOSS_LIMIT`, default **$100**):
  - The first account reading of each ET day is that day's starting value. It
    is IBKR's net liquidation value.
  - The link checks again once a minute, plus at every bar check. If the day's
    loss reaches the limit, it does the following:
    1. Cancels its working orders.
    2. Closes every position this dashboard manages.
    3. Sends nothing more that day.
  - The day's loss is the worse of two measures: the drop since the starting
    value, and IBKR's own daily P&L.
  - Commissions and fees count toward the loss.
  - The halt clears by itself the next day. The next sync then brings the
    account back to the model's position. Since Live symbols are flat by
    15:55, that means a new short only on a new signal.
- **Kill switch** (broker card on the dashboard). Both settings survive restarts
  until you press **Resume**.
  - **Pause orders**: sends nothing at all, neither entries nor exits.
    Positions stay as they are.
  - **Flatten & stop**: cancels working orders, closes managed positions now,
    then sends nothing.
  - **Resume**: the next sync re-opens whatever the model still holds.
- **Short check before every new short.**
  - The link asks IBKR for shortable shares. This is IBKR's "shortable" tick.
  - If IBKR shows no shares to borrow, it skips the short. The skip is logged
    once a day and shown in the broker card. The model keeps the trade.
  - If IBKR sends no short data, the order goes out and IBKR's own locate check
    accepts or rejects it.
  - Rejected shorts are not re-sent that day. Covers are never blocked.
- **Order limits enforced inside the IBKR adapter.** These are independent of
  the rest of the app:
  - No order may take a symbol past the share cap.
  - No order may be worth more than `IBKR_MAX_ORDER_USD` (default $1,000).
  - An order with no price to check against is refused.
  - Orders that only reduce a position are always allowed.
- **Orders**:
  - Day, regular hours only.
  - **Marketable limit orders**: the ask (buy) or bid (sell), plus a 3% collar.
    If there is no quote, the model's last price is used.
  - A limit order still unfilled after 45 s is cancelled and re-priced about a
    minute later.
  - `IBKR_ORDER_TYPE=market` switches to plain market orders.
- **Borrow fee is not read from IBKR.** The API exposes availability, not the fee.
  - Keep entering each symbol's fee with the **Borrow** button, for example
    from iBorrowDesk.
  - The model's overnight borrow limit (200%) then makes those symbols flat by
    15:55. The account follows the model.
- **Only Live symbols (and ones it traded before) are touched.** Anything else
  in the account is left alone. That includes manual trades and their orders.
- **The account ID sets the mode.** `IBKR_MODE=paper` needs a `DU…` paper
  account and `IBKR_MODE=live` needs a `U…` live account. IB Gateway must be
  logged in to exactly that account, or nothing is sent.

## Set up (once the account is funded)

### 1. IBKR account settings (Client Portal)

- **Margin account.** Shorting needs margin; a cash account cannot short.
- **Trading permissions:** US stocks.
- **Market data:** a US real-time subscription for API quotes. For example,
  the "US Securities Snapshot and Futures Value Bundle"; check the current
  price and the waiver in Client Portal. Without one, set
  `IBKR_MARKET_DATA_TYPE=3` for delayed quotes. Limit prices are then based on
  15-minute-old quotes, so `IBKR_ORDER_TYPE=market` may suit better.
- **Recommended: a second username for the API.** Client Portal → Settings →
  Users & Access Rights.
  - Only one session per username can trade at a time. Logging in to TWS or
    the mobile app with the gateway's username can disconnect the gateway.
  - Use your main login on your phone and the second one for the gateway.
- **The paper account** (DU…) is available in Client Portal. Start there.

### 2. IB Gateway on the server (Docker)

On the droplet, as root:

```bash
curl -fsSL https://get.docker.com | sh              # Docker, if not installed
free -h                                             # the gateway needs ~1 GB of RAM free
mkdir -p /opt/ibgateway && cd /opt/ibgateway
cp /opt/tranche/autoagent/tranche-dashboard/deploy/ib-gateway/compose.yml .
nano .env                                           # see below
chmod 600 .env
docker compose up -d
docker compose logs -f                              # wait until the login has finished (Ctrl-C to leave)
```

`/opt/ibgateway/.env`:

```
TWS_USERID=your-ibkr-username
TWS_PASSWORD=your-ibkr-password
TRADING_MODE=paper
```

**2FA (live):**

- When the gateway logs in to a live account, approve the IBKR Mobile
  notification on your phone.
- The gateway restarts each night at 11:45 PM ET without a new login.
  Expect to approve 2FA again about once a week, after IBKR's weekly
  re-authentication.
- If a 2FA prompt is missed, the gateway retries.
- The dashboard shows "can't connect to IB Gateway" while it is logged out,
  and sends nothing during that time.

The API ports are bound to `127.0.0.1` only:

| Port | Account |
|---|---|
| 4002 | paper |
| 4001 | live |

### 3. The dashboard

Add these lines to `/opt/tranche/autoagent/tranche-dashboard/.env`:

```
IBKR_ACCOUNT=DU1234567
IBKR_MODE=paper
```

Then run `systemctl restart tranche` and check the startup lines with
`journalctl -u tranche -n 40 --no-pager`:

```
IBKR paper: OK - DU1234567 net liquidation $1,000,000.00; cap 1 share(s) per symbol, daily loss limit $100; 0 live symbol(s)
```

On the dashboard, the **IBKR paper account** card shows on every book tab.

1. On the **5-min** tab, on each row you want traded, click **Live: off**,
   tick **5/10 EMA cross (short)**, then click **Save**. The row shows a red
   pill: `IBKR 5/10 short · flat 3:55`.
2. Turn on **Send the Live symbols' trades to IBKR paper** in the IBKR card.

### 4. Paper test, then live

1. **Paper test.** Run on paper for at least a day.
   - Positions should match the model's direction.
   - Shorts show IBKR's borrow availability.
   - Rejections stop after the first one each day.
   - Press **Flatten & stop** once, then **Resume**.
2. **Switch the gateway to live.** Set `TRADING_MODE=live` in
   `/opt/ibgateway/.env`, then run `docker compose up -d` and approve 2FA.
3. **Switch the dashboard to live.** In the dashboard `.env`, set:

   ```
   IBKR_ACCOUNT=U…
   IBKR_MODE=live
   ```

   Then run `systemctl restart tranche`.
4. **Start live orders.** Switching orders on for a LIVE account requires
   typing **LIVE**. The card shows a red **LIVE · real money** badge. Your
   Live selections carry over from the paper test.

## Costs at one share

These figures are approximate, from IBKR's published rates. Check your plan.

| Plan | Commission |
|---|---|
| IBKR Pro Tiered | $0.0035/share, $0.35 minimum per order |
| IBKR Pro Fixed | $0.005/share, $1 minimum per order |

- Regulatory fees are extra.
- At one share, the minimum dominates. Twenty round trips a day cost roughly
  $14–$40, and that counts toward the $100 limit.
- Borrow is charged per night held. A same-day short generally pays no borrow
  fee if it settles the same day and no pre-borrow applies.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `IBKR_ACCOUNT` | (off) | `DU…` paper or `U…` live account ID |
| `IBKR_MODE` | `paper` | `paper` or `live`; must match the account ID |
| `IBKR_BOOK` | `5m` | The one book whose Live symbols trade at IBKR: `1h`, `15m` or `5m` |
| `IBKR_MAX_SHARES` | 1 | Shares per symbol. The only size setting. Short only, so −1 by default |
| `IBKR_DAILY_LOSS_LIMIT` | 100 | $ loss in a day that halts and flattens (live: must be > 0) |
| `IBKR_MAX_ORDER_USD` | 1000 | Max value of one order |
| `IBKR_ORDER_TYPE` | `limit` | `limit` (marketable, collared) or `market` |
| `IBKR_LIMIT_COLLAR_PCT` | 3 | How far through the bid/ask the limit goes |
| `IBKR_REPRICE_S` | 45 | Cancel and re-price an unfilled limit after this many seconds |
| `IBKR_MARKET_DATA_TYPE` | 1 | 1 real-time, 3 delayed |
| `IBKR_HOST` / `IBKR_PORT` | 127.0.0.1 / 4002 or 4001 | Where IB Gateway listens |
| `IBKR_CLIENT_ID` | 17 | API client ID; must be unique per connection |

## Not covered

- **No resting stop orders at IBKR.** Exits happen at the 5-min book's checks
  (every 5 minutes, 2 minutes after each bar closes), as in the model. A move
  between checks is not protected. The loss limit is checked once a minute;
  its covers, like every cover except the stop, are marketable limit orders.
- **No pre-borrow and no locate purchase.** If IBKR can't borrow a stock, that
  short is skipped.
- **No outside-hours orders.** A flatten after 16:00 ET waits for the next open.
- **The day's starting value is taken at the first reading of the day.** That
  is normally before the open. If the server starts mid-session after a loss,
  IBKR's own daily P&L still catches it.
