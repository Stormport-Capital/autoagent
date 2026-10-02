# Interactive Brokers link (paper or LIVE)

One book (the one you name in `IBKR_BOOK`) can trade an Interactive Brokers
account instead of an Alpaca paper account. The same sync logic is used, with
extra guards for real money. The other two books keep their Alpaca paper links.

## What it does

- **It follows the direction, not the size.** With the default `IBKR_MAX_SHARES=1`,
  the account holds **−1, 0 or +1 share** of each symbol: short when the model's
  net position is short, long when it's long, flat when it's flat. So the live
  P&L is **not** the model's P&L. It tests the plumbing: signals, orders,
  borrow, fills and fees.
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
    account back to the model's positions.
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
- **Only symbols in this book are touched.** Anything else in the account is
  left alone. That includes manual trades and their orders.
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
IBKR_BOOK=15m
```

Then run `systemctl restart tranche` and check the startup lines with
`journalctl -u tranche -n 40 --no-pager`:

```
IBKR paper (15-min): OK - DU1234567 net liquidation $1,000,000.00; cap 1 share(s), daily loss limit $100
```

On the dashboard, open the book's tab. The broker card shows
**IBKR paper account**. Turn on **Send the model's trades to IBKR paper**.

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
4. **Start live orders.** Switching orders on for a LIVE book requires typing
   **LIVE**. The card shows a red **LIVE · real money** badge.

Only one book can trade the IBKR account. IBKR nets one position per symbol per
account, so two books would fight each other.

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
| `IBKR_BOOK` | (required) | `1h`, `15m` or `5m` |
| `IBKR_MAX_SHARES` | 1 | Max shares per symbol, either direction |
| `IBKR_DAILY_LOSS_LIMIT` | 100 | $ loss in a day that halts and flattens (live: must be > 0) |
| `IBKR_MAX_ORDER_USD` | 1000 | Max value of one order |
| `IBKR_ORDER_TYPE` | `limit` | `limit` (marketable, collared) or `market` |
| `IBKR_LIMIT_COLLAR_PCT` | 3 | How far through the bid/ask the limit goes |
| `IBKR_REPRICE_S` | 45 | Cancel and re-price an unfilled limit after this many seconds |
| `IBKR_MARKET_DATA_TYPE` | 1 | 1 real-time, 3 delayed |
| `IBKR_HOST` / `IBKR_PORT` | 127.0.0.1 / 4002 or 4001 | Where IB Gateway listens |
| `IBKR_CLIENT_ID` | 17 | API client ID; must be unique per connection |

## Not covered

- **No resting stop orders at IBKR.** Exits happen at the book's bar checks,
  as in the model. A gap between checks is not protected. The loss limit is
  checked once a minute and acts at market.
- **No pre-borrow and no locate purchase.** If IBKR can't borrow a stock, that
  short is skipped.
- **No outside-hours orders.** A flatten after 16:00 ET waits for the next open.
- **The day's starting value is taken at the first reading of the day.** That
  is normally before the open. If the server starts mid-session after a loss,
  IBKR's own daily P&L still catches it.
