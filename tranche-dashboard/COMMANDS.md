# Droplet commands (copy and paste)

Run these in the droplet console (simple-short), logged in as root. Every
command is one line unless shown otherwise. Nothing here sends an order,
except where a line says so.

## Start here, every time

```bash
cd /opt/tranche/autoagent/tranche-dashboard
```

## The dashboard

Restart. This also pulls the latest merged code first. Safest after the 4:00 PM
close; avoid 9:25 to 9:50 AM ET.

```bash
systemctl restart tranche
```

Is it running? Look for `active (running)`.

```bash
systemctl status tranche --no-pager
```

The last 60 log lines, including the connection check printed at startup:

```bash
journalctl -u tranche -n 60 --no-pager
```

Watch the log live. Press Ctrl+C to stop watching; the dashboard keeps running.

```bash
journalctl -u tranche -f
```

Which code version is running (the newest merge is first):

```bash
sudo -u tranche git -C /opt/tranche/autoagent log --oneline -3
```

When the automatic weekday 8:45 AM ET update-and-restart runs next:

```bash
systemctl list-timers tranche-update.timer --no-pager
```

## TradeZero

Can a stock be shorted? Replace BNAI with the ticker. This is read-only: it
shows whether the stock is easy to borrow and whether shares are already
located.

```bash
sudo -u tranche /opt/tranche/venv/bin/python -I tz_check.py BNAI
```

The same check plus the locate price for 100 shares, as a % of the share
price. It asks TradeZero for a quote only; nothing is accepted or bought.

```bash
sudo -u tranche /opt/tranche/venv/bin/python -I tz_check.py BNAI --locate-quote
```

How we're really doing at TradeZero: every fill, fee, locate and borrow
charge, matched against the dashboard. Read-only.

```bash
sudo -u tranche /opt/tranche/venv/bin/python -I tz_reconcile.py
```

The same report from a chosen start date:

```bash
sudo -u tranche /opt/tranche/venv/bin/python -I tz_reconcile.py --since 2026-11-11
```

## Settings (.env)

List the setting names only. No values are shown, so no keys appear on screen.

```bash
grep -o '^[A-Z_0-9]*=' .env
```

Show the TradeZero trading settings (no keys):

```bash
grep -E '^(LIVE_BROKER|TZ_ACCOUNT|TZ_MODE|TZ_BOOKS|TZ_SHARES|TZ_MAX_SHARES|TZ_MAX_ORDER_USD|TZ_DAILY_LOSS_LIMIT|TZ_LOCATE_MAX_PCT|TZ_LOCATE_MAX_USD_DAY)=' .env
```

Edit settings. Use the arrow keys to move. Save with Ctrl+O then Enter, and
exit with Ctrl+X. Then restart the dashboard (above) so it reads the change.
Never paste API keys into a chat.

```bash
nano .env
```

## The server itself

Free disk space:

```bash
df -h /
```

Reboot the server when the login says "System restart required". Do this only
after the 4:00 PM close. First check that the dashboard starts again on its
own: it must print `enabled`.

```bash
systemctl is-enabled tranche
```

```bash
reboot
```

After about a minute, log back in and run the "Is it running?" check above.

## In the browser (on Tailscale)

The dashboard:

```
http://tranche:8050
```

Every bar the engine used for one symbol, with its EMAs, VWAP and signals.
Change `5m` to `15m`, `1h` or `1d` and `BNAI` to the ticker. Add `.csv` to the
end to download it instead.

```
http://tranche:8050/api/5m/bars/BNAI
```
