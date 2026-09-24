# Setup: Polygon data + Alpaca paper account

About 20 minutes, one time. You need three keys:

| Name (exact spelling) | Where it comes from | What it's for |
|---|---|---|
| `POLYGON_API_KEY` | Polygon dashboard | Price and volume data (EMAs, VWAP) |
| `APCA_API_KEY_ID` | Alpaca **paper** dashboard | Paper trading account |
| `APCA_API_SECRET_KEY` | Alpaca **paper** dashboard (shown once) | Paper trading account |

**Where the keys go:** a file named `.env` in the `tranche-dashboard` folder on
the computer that runs the app. They do **not** go into GitHub secrets. The app
runs on your machine, not on GitHub, so GitHub secrets would never reach it.
Don't paste keys into chat, email or any committed file. `.env` is git-ignored.

---

## 1. Polygon API key

Polygon renamed itself **Massive** in late 2025. The old polygon.io links
redirect, and the key and API work the same way.

1. Sign in at <https://polygon.io/dashboard> (it may redirect to massive.com).
2. In the left menu, open **Keys** (or **API Keys**): <https://polygon.io/dashboard/keys>.
3. Copy the default key, or click **New Key** and name it `tranche-dashboard`.
4. **Check your plan** (dashboard → **Subscriptions** / **Billing**). The app
   needs same-day intraday bars:
   - **Free/Basic**: won't work. It has no same-day data, so the app would never
     see the current session.
   - **Starter / Developer** tiers: work, but the data is **15 minutes delayed**.
     Each hourly check then happens about 15–20 minutes after the bar closes.
     The app waits for the full hour of data and never trades a half-finished bar.
   - **Advanced** (real-time): checks happen about 2 minutes after each bar closes.

   Tier names and what they include change. If unsure, the plan page lists
   whether stocks data is "real-time" or "15-minute delayed".

## 2. Alpaca paper account and keys

1. Create an account at <https://app.alpaca.markets/signup> and verify your
   email. A paper account needs no funding and no identity documents.
2. Open the **paper** dashboard: <https://app.alpaca.markets/paper/dashboard/overview>.
   The account switcher in the top-left must say **Paper**, not Live. Paper and
   live keys are different, and this app only accepts paper.
3. **Set the paper balance to match the dashboard's portfolio size** (the app
   defaults to $100,000). From the paper account menu (top-left), choose
   reset/new paper account and enter the amount. Keep it at **$25,000 or more**:
   below that, pattern-day-trader rules can block same-day round trips.
4. On the paper dashboard's home page, find the **API Keys** panel (right side)
   and click **Generate New Keys**. If you already had keys, click **Regenerate**.
5. Copy both values **now**:
   - **API Key ID** → `APCA_API_KEY_ID`
   - **Secret Key** → `APCA_API_SECRET_KEY`. It's shown only once; if you lose
     it, regenerate.

## 3. Get the code and put the keys in `.env`

You need **Python 3.11 or newer**: <https://www.python.org/downloads/>. On
Windows, tick "Add Python to PATH" in the installer.

```bash
git clone https://github.com/Stormport-Capital/autoagent.git
cd autoagent
git checkout claude/dashboard-stock-trading-9iy2hb   # until the PR is merged
cd tranche-dashboard
pip install -r requirements.txt
```

Create `.env` from the template:

- **Mac/Linux:** `cp .env.example .env`, then `open -e .env` (Mac) or `nano .env`
- **Windows (PowerShell):** `copy .env.example .env`, then `notepad .env`

Fill it in. No quotes and no spaces around `=`. Paste each key **inside the
file**, after its `=`. Don't type anything into Notepad's "File name" box: the
file name stays exactly `.env`.

```
POLYGON_API_KEY=abc123...
APCA_API_KEY_ID=PK...
APCA_API_SECRET_KEY=...
```

(Alpaca paper key IDs usually start with `PK`.)

## 4. Run it: double-click

In the `tranche-dashboard` folder, double-click **`Start Dashboard.bat`**
(Windows) or **`Start Dashboard.command`** (Mac). It:
1. gets the latest version and installs anything missing;
2. on the very first run, creates `.env` and opens it in Notepad for your keys;
3. starts the dashboard and opens it in your browser.

**Keep the black window open while it runs; close it to stop.** If you
double-click again while it's already running, it just opens the browser. A
second copy never starts.

For a desktop icon on Windows, right-click `Start Dashboard.bat` → **Show more
options** → **Send to** → **Desktop (create shortcut)**.

The black window lists each key as `found`, `EMPTY` or `missing` (never the
key itself), then shows `provider=polygon` and `alpaca paper=linked` once
everything is in place.

(From a terminal, `python app.py --open` does the same thing.)

In the dashboard:
1. **Portfolio settings**: set the portfolio size (match your paper balance)
   and save.
2. **Add symbols**: enter them, pick *Short only* or *Long & short*, and set risk.
3. **Alpaca paper account** card: check that the paper equity shows up. Then
   tick **"Send the model's trades to Alpaca paper."** Until you tick it, the
   model trades only on its own books.

## 5. Keep it running

The app has to be running during market hours (9:30–16:00 ET) to check each
hour and send orders.
- **On your computer:** leave the terminal open and stop the computer from
  sleeping during market hours.
- **Always-on alternative:** any small Linux VM works. Copy the folder and
  `.env` there and run `python app.py` under `systemd` or `tmux`.

If it was off, on restart it catches up on the hours it missed. The model fills
those at the historical bar closes, and then sends the paper account whatever
orders are needed to match.

## How the paper link behaves

- **Paper only.** The paper endpoint `paper-api.alpaca.markets` is hard-coded.
  Any other address raises an error before sending anything. There is no live
  setting.
- **Orders:** after each hourly check, the app compares each symbol's model net
  position (all tranches added up) with the paper position. It sends a market
  order for the difference. A switch from long to short goes out as two orders:
  close, then open.
- **Only your symbols:** it only touches symbols you added in the app. Anything
  else in the paper account is left alone. Removing a symbol, or resetting the
  portfolio, also closes that position at Alpaca.
- **Stops are hourly, like the model.** There are no resting stop orders at
  Alpaca, so a stock can move past a stop inside the hour, just as in the model.
- **After the close:** a signal on the 15:30–16:00 bar is checked at about
  16:02. Its order queues and fills at the **next day's open**, while the model
  books it at the 16:00 close.
- **Shorting:** the model assumes every stock can be borrowed. Alpaca may still
  **reject** shorts in hard-to-borrow names. A rejected order shows in the
  **Paper orders** tab, and the symbol shows as a mismatch on the Alpaca card.
- **Borrow fees:** the model charges its 10%/yr. As far as I know, Alpaca paper
  doesn't charge borrow fees, so paper P&L won't include them.
- The equity chart shows both lines, model and Alpaca paper. The Alpaca card
  shows paper equity, today's P&L and a model-vs-paper position check for each
  symbol.

## Daily use: when to add symbols

The app checks every watched symbol right after each hourly bar closes:
**10:30, 11:30, 12:30, 13:30, 14:30, 15:30 and 16:00 ET**, plus a couple of
minutes of delay. With a 15-minute-delayed data plan, it keeps re-checking every
3 minutes until the full hour of data has arrived.

- **Add symbols whenever you like.** Only hours that finish *after* you add a
  symbol can trigger trades, so it never acts on old signals.
- **Added before the open:** the first check is at about 10:32, on the
  9:30–10:30 bar. The EMA lines already carry the previous days' history, so an
  EMA cross can trigger at that first check. The VWAP tranche needs an earlier
  hour of *today* that closed above VWAP, so its earliest possible entry is the
  11:30 check.
- **Added after the open** (say 11:05): the first check is at 11:32. The hours
  already finished aren't traded, but they still count as today's history for
  VWAP and the high of day. A cross that already happened before you added the
  symbol is not acted on; only the next new cross is.
- **Added after 16:00:** nothing happens until about 10:32 the next trading day.
- **Symbols stay on the watchlist** until you click **Remove**. You don't
  re-enter them each day. **Pause** stops new entries but still manages open
  positions. **Flatten** closes that symbol's positions now.
- **Positions carry overnight**:
  - EMA tranches: until the opposite cross or the stop.
  - VWAP tranche: until a new high of day above entry, or the 10-day average.
- **Paper orders** go out right after each check. Orders from the 16:00 check
  queue and fill at the next day's open.
- **If the app was closed** during checks, it catches up when you start it. The
  model books those missed hours at their historical closes; the paper account
  can only trade at the current price. So the two can differ after downtime.
- **Check now** re-runs the check immediately. It never duplicates a trade.
