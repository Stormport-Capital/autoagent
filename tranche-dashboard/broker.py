"""Alpaca PAPER trading link.

The engine's tranches stay the source of truth for signals, sizing and
per-tranche performance. After every hourly check, BrokerSync drives the Alpaca
paper account's position in each watched symbol to the model's NET quantity
(sum of the open tranches: longs positive, shorts negative), with market orders.

  * PAPER ONLY. The endpoint is fixed to paper-api.alpaca.markets; any other
    host raises before a request is made. There is no live-trading setting.
  * Only symbols added in this app are touched. Anything else in the paper
    account (e.g. manual trades) is left alone.
  * A symbol with an order still working is skipped until that order finishes,
    so a slow fill can never cause a duplicate order.
  * Long -> short (or back) is done in two legs: close, wait for the fill, then
    open. Alpaca rejects a single order that crosses through zero.
  * Stops are the model's hourly stops, so the paper account exits at the same
    hourly checks as the model. There are no resting stop orders at Alpaca.
  * A refusal is not retried blindly. "Cannot be sold short" / not shortable /
    not easy to borrow stops new or larger shorts in that symbol until the next
    session; "not found" / not tradable stops every order in it until the next
    session; a trading halt pauses it for 10 minutes. Covers and long exits are
    never held back by a short refusal. Each skip is logged once per day.

The same BrokerSync drives the IBKR link (ibkr.py), with three extra guards
that are on whenever max_shares / daily_loss_limit are given:
  * Share cap: the account mirrors the SIGN of the model's net position, capped
    at max_shares per symbol (1 = one-share tests).
  * Daily loss limit: the first equity reading of each ET day is the day's
    start. When equity (or IBKR's own daily P&L, whichever is worse) is down
    by the limit, the link latches HALTED for the rest of that day: working
    orders are cancelled, managed positions are closed, nothing else is sent.
    It clears by itself the next day.
  * Kill switch (dashboard): "pause" sends nothing at all and leaves positions
    as they are; "flatten" cancels working orders, closes managed positions and
    then sends nothing. Both persist across restarts until Resume.
"""

from __future__ import annotations

import os
import re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

from indicators import ET

from data import redact

PAPER_URL = "https://paper-api.alpaca.markets"
PAPER_HOST = "paper-api.alpaca.markets"
FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "failed"}


class BrokerError(RuntimeError):
    pass


def _msg(e: Exception) -> str:
    if isinstance(e, requests.RequestException):
        return f"can't reach Alpaca paper ({type(e).__name__}); will retry on the next check"
    return redact(e)


class AlpacaPaper:
    def __init__(self, key: str, secret: str, base_url: str = PAPER_URL, session=None):
        if urlparse(base_url).hostname != PAPER_HOST:
            raise BrokerError(f"refusing non-paper Alpaca endpoint {base_url!r}")
        self.base = base_url.rstrip("/")
        self.http = session or requests.Session()
        self.http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    @classmethod
    def from_env(cls, prefix: str = "APCA") -> "AlpacaPaper | None":
        """APCA_API_KEY_ID / APCA_API_SECRET_KEY for the hourly book;
        APCA_15M_API_KEY_ID / APCA_15M_API_SECRET_KEY for the 15-minute book;
        APCA_5M_API_KEY_ID / APCA_5M_API_SECRET_KEY for the 5-minute book."""
        key = os.environ.get(f"{prefix}_API_KEY_ID")
        secret = os.environ.get(f"{prefix}_API_SECRET_KEY")
        return cls(key, secret) if key and secret else None

    def _req(self, method: str, path: str, **kw):
        url = self.base + path
        if urlparse(url).hostname != PAPER_HOST:  # belt and braces
            raise BrokerError("refusing non-paper Alpaca endpoint")
        r = self.http.request(method, url, timeout=20, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise BrokerError(f"Alpaca {r.status_code}: {str(msg)[:300]}")
        return r.json() if r.content else None

    def account(self) -> dict:
        return self._req("GET", "/v2/account")

    def positions(self) -> dict[str, dict]:
        return {p["symbol"]: p for p in self._req("GET", "/v2/positions")}

    def open_orders(self) -> list[dict]:
        return self._req("GET", "/v2/orders", params={"status": "open", "limit": 500})

    def get_order(self, order_id: str) -> dict:
        return self._req("GET", f"/v2/orders/{order_id}")

    def submit(self, symbol: str, qty: int, side: str, client_order_id: str) -> dict:
        return self._req("POST", "/v2/orders", json={
            "symbol": symbol, "qty": str(qty), "side": side, "type": "market",
            "time_in_force": "day", "client_order_id": client_order_id})


NO_SHORT = re.compile(r"cannot be sold short|not shortable|not easy to borrow|no shares available|"
                      r"not available for short|no shares to borrow", re.I)
NO_TRADE = re.compile(r"not found|not tradable|not active", re.I)
HALTED = re.compile(r"halt", re.I)
HALT_PAUSE = timedelta(minutes=10)


KILL_MODES = ("pause", "flatten")
WATCH_EVERY_S = 60


class BrokerSync:
    def __init__(self, store, broker, fill_wait_s: float = 20.0, max_shares: int | None = None,
                 daily_loss_limit: float | None = None):
        self.store, self.broker = store, broker
        self.fill_wait_s = fill_wait_s
        self.max_shares = max_shares
        self.daily_loss_limit = daily_loss_limit or None
        self.venue = getattr(broker, "venue", "Alpaca paper")
        self.live = bool(getattr(broker, "live", False))
        self.short_notes: dict[str, tuple] = {}  # symbol -> (ET day, why no short)
        self._watched: float | None = None  # monotonic time of the last loss check
        self.lock = threading.Lock()
        self.last_sync: datetime | None = None
        self.last_error: str | None = None
        self.snapshot: dict | None = None      # cached account + positions for the UI
        self.snapshot_at = 0.0
        self.needs_followup = False            # an order was working: re-sync soon

    # ---------------------------------------------------------------- sync
    def _net(self) -> dict[str, int]:
        """The model's net shares per symbol (longs positive, shorts negative)."""
        want: dict[str, int] = {}
        for t in self.store.q("SELECT symbol, side, qty FROM tranches WHERE status='open'"):
            want[t["symbol"]] = want.get(t["symbol"], 0) + (t["qty"] if t["side"] == "long" else -t["qty"])
        return want

    def _renamed(self) -> dict[str, str]:
        """New ticker -> old ticker, for symbols renamed in the app."""
        return {r["symbol"]: r["renamed_from"] for r in self.store.q(
            "SELECT symbol, renamed_from FROM symbols WHERE renamed_from IS NOT NULL")}

    def _recon_extra(self, sym: str) -> dict:
        return {}

    def desired(self) -> dict[str, int]:
        want = self._net()
        if self.max_shares:  # same direction as the model, at most max_shares
            want = {s: max(-self.max_shares, min(self.max_shares, q)) for s, q in want.items()}
        return want

    def managed(self) -> set[str]:
        return {r["symbol"] for r in self.store.q("SELECT DISTINCT symbol FROM symbols")}

    def sync(self, now: datetime) -> None:
        with self.lock:
            try:
                self._refresh_orders()
                acct = self.broker.account()
                self._check_loss(now, float(acct["equity"]))
                stop = self.stop_reason(now)
                if stop and stop[0] == "pause":
                    self._log_skip(now, None, stop[1])
                else:
                    self._sync_positions(now, flatten=stop is not None)
                acct = self.broker.account()
                self.store.x("INSERT OR REPLACE INTO broker_equity VALUES (?, ?)",
                             (now.isoformat(), float(acct["equity"])))
                self.last_error = None
            except (BrokerError, requests.RequestException) as e:
                self.last_error = _msg(e)
                self.store.log(now, "broker-error", self.last_error[:300])
            self.last_sync = now
            self.snapshot_at = 0.0  # force a fresh account read

    def _sync_positions(self, now: datetime, flatten: bool) -> None:
        if flatten:  # kill switch or loss limit: nothing may still be working
            for sym, o in self._our_working().items():
                if sym in self.managed():
                    self._cancel(now, sym, o, "closing everything")
        want = {} if flatten else self.desired()
        pos = {s: int(float(p["qty"])) for s, p in self.broker.positions().items()}
        busy = {o["symbol"]: o for o in self.broker.open_orders()}
        self.needs_followup = False
        # a renamed ticker waits until the account has converted the old one
        waiting = {new: old for new, old in self._renamed().items() if pos.get(old)}
        for sym in sorted(self.managed() | set(want)):
            target, cur = want.get(sym, 0), pos.get(sym, 0)
            if self.max_shares and abs(target) > self.max_shares:  # never: desired() caps it
                raise BrokerError(f"{sym}: target {target:+d} is above the share cap")
            if sym in waiting:
                if target != cur:
                    self.store.log(now, "broker-wait", f"{self.venue} still holds "
                                   f"{waiting[sym]} {pos[waiting[sym]]:+d}; no {sym} order "
                                   f"until the broker converts it (or close {waiting[sym]} "
                                   f"in the broker's app)", sym)
                continue
            if target == cur:
                continue
            if sym in busy:
                self.needs_followup = True
                self._reprice(now, sym, busy[sym])
                continue
            why = self._held_back(now, sym, opens_short=False)
            if why:
                self._log_skip(now, sym, why)
                continue
            if cur and target and (cur > 0) != (target > 0):
                order = self._submit(now, sym, -cur, f"close {cur:+d} before reversing")
                if not order or not self._await_fill(order):
                    self.needs_followup = order is not None
                    continue  # opening leg goes out on a follow-up sync
                cur = 0
            opens_short = target < 0 and target < min(cur, 0)
            why = self._held_back(now, sym, opens_short=opens_short)
            if not why and opens_short:
                why = self._short_check(now, sym, min(cur, 0) - target)
            if why:
                self._log_skip(now, sym, why)
                continue
            self._submit(now, sym, target - cur,
                         "kill switch / loss limit: close" if flatten
                         else f"model {target:+d}, {self.venue} {cur:+d}",
                         order_type=None if flatten else self._order_type(sym, target, cur))

    def _order_type(self, sym: str, target: int, cur: int) -> str | None:
        """Order type for this order, or None for the broker's default."""
        return None

    def _short_check(self, now: datetime, sym: str, qty: int) -> str | None:
        check = getattr(self.broker, "short_check", None)
        if check is None:
            return None
        why = check(sym, qty)
        if why:
            self.short_notes[sym] = (now.astimezone(ET).date(), why)
        else:
            self.short_notes.pop(sym, None)
        return why

    def _our_working(self) -> dict[str, dict]:
        """Working broker orders this app sent (never someone else's)."""
        ours = {r["broker_order_id"] for r in self.store.q(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
        return {o["symbol"]: o for o in self.broker.open_orders() if o["id"] in ours}

    def _cancel(self, now: datetime, sym: str, o: dict, why: str) -> None:
        cancel = getattr(self.broker, "cancel", None)
        if cancel is None:
            return
        cancel(o["id"])
        self.store.log(now, "broker", f"cancelled working order {o['id']} ({why})", sym)

    def _reprice(self, now: datetime, sym: str, o: dict) -> None:
        """A limit order of ours still unfilled after reprice_s is cancelled;
        the follow-up sync sends a fresh one at the new quote."""
        wait = getattr(self.broker, "reprice_s", 0)
        if not wait:
            return
        row = self.store.q("SELECT ts FROM orders WHERE broker_order_id=?", (o["id"],))
        if row and (now - datetime.fromisoformat(row[0]["ts"])).total_seconds() >= wait:
            self._cancel(now, sym, o, f"unfilled after {wait}s; re-pricing")

    # ------------------------------------------------------------ live guards
    def _ctl(self, key: str) -> str | None:
        r = self.store.q("SELECT value FROM broker_control WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def _set_ctl(self, key: str, value: str | None) -> None:
        if value is None:
            self.store.x("DELETE FROM broker_control WHERE key=?", (key,))
        else:
            self.store.x("INSERT OR REPLACE INTO broker_control VALUES (?, ?)", (key, value))

    def set_kill(self, mode: str | None, now: datetime) -> None:
        """'pause', 'flatten', or None (resume). Persists across restarts."""
        if mode not in (None, *KILL_MODES):
            raise BrokerError("kill switch mode must be pause, flatten or off")
        self._set_ctl("kill", mode)
        self.store.log(now, "broker-kill", {
            None: f"kill switch off: orders to {self.venue} resume at the next sync",
            "pause": f"kill switch: PAUSED, nothing is sent to {self.venue}; positions stay",
            "flatten": f"kill switch: FLATTEN, closing managed {self.venue} positions; "
                       "nothing else is sent"}[mode])

    def day_start(self, now: datetime) -> float | None:
        r = self._ctl("day_start")
        if r:
            day, eq = r.split("|")
            if day == now.astimezone(ET).date().isoformat():
                return float(eq)
        return None

    def _check_loss(self, now: datetime, equity: float) -> bool:
        """Latch the day's start equity; halt for the day at the loss limit.
        True when it has just tripped."""
        day = now.astimezone(ET).date().isoformat()
        start = self.day_start(now)
        if start is None:
            self._set_ctl("day_start", f"{day}|{equity}")
            start = equity
        if not self.daily_loss_limit or (self._ctl("halt") or "").startswith(day + "|"):
            return False
        pl = equity - start
        own = getattr(self.broker, "day_pnl", None)
        if own is not None:
            ib = own()
            if ib is not None:
                pl = min(pl, ib)
        if pl > -self.daily_loss_limit:
            return False
        why = (f"daily loss limit hit: {self.venue} day P&L {pl:+,.2f} vs "
               f"-{self.daily_loss_limit:,.0f}")
        self._set_ctl("halt", f"{day}|{why}")
        self.store.log(now, "broker-halt", why + "; closing managed positions, no more orders "
                       "today (the model keeps trading)")
        return True

    def stop_reason(self, now: datetime) -> tuple[str, str] | None:
        kill = self._ctl("kill")
        if kill:
            return kill, f"kill switch is on ({kill}); Resume on the dashboard to send orders"
        halt = self._ctl("halt") or ""
        day = now.astimezone(ET).date().isoformat()
        if halt.startswith(day + "|"):
            return "flatten", halt.split("|", 1)[1] + "; orders resume tomorrow"
        return None

    def watch(self, now: datetime) -> None:
        """Called every scheduler pass: re-checks the loss limit once a minute
        between bar checks, and flattens straight away if it trips."""
        if not self.daily_loss_limit or (self._watched is not None
                                         and _time.monotonic() - self._watched < WATCH_EVERY_S):
            return
        self._watched = _time.monotonic()
        try:
            with self.lock:
                tripped = self._check_loss(now, float(self.broker.account()["equity"]))
        except (BrokerError, requests.RequestException):
            return  # the next sync reports it
        if tripped:
            self.sync(now)

    def _held_back(self, now: datetime, sym: str, opens_short: bool) -> str | None:
        """Why an order for `sym` should not be sent right now, from its latest
        rejection (None = go ahead). A filled or working order after the
        rejection clears it."""
        rows = self.store.q("SELECT ts, side, status, message FROM orders WHERE symbol=? "
                            "ORDER BY id DESC LIMIT 20", (sym,))
        today = now.astimezone(ET).date()
        for r in rows:
            if r["status"] != "rejected":
                if not (opens_short and r["side"] == "buy"):
                    return None  # a later accepted order supersedes older refusals
                continue  # a cover filling doesn't prove we can short again
            msg = r["message"] or ""
            when = datetime.fromisoformat(r["ts"])
            if HALTED.search(msg) and now - when < HALT_PAUSE:
                return f"trading halt - retrying after {(when + HALT_PAUSE).astimezone(ET):%H:%M}"
            if when.astimezone(ET).date() != today:
                return None
            if NO_TRADE.search(msg):
                return f"{self.venue} won't trade it today ({msg[:80]})"
            if opens_short and r["side"] == "sell" and NO_SHORT.search(msg):
                return f"{self.venue} can't short it today ({msg[:80]})"
        return None

    def _log_skip(self, now: datetime, sym: str | None, why: str) -> None:
        day_start = datetime.combine(now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET)
        seen = self.store.q("SELECT ts, message FROM events WHERE kind='broker-skip' AND "
                            "symbol IS ? ORDER BY id DESC LIMIT 1", (sym,))
        if seen and datetime.fromisoformat(seen[0]["ts"]) >= day_start and why in seen[0]["message"]:
            return
        self.store.log(now, "broker-skip", f"not sent to {self.venue}: {why}. The model keeps "
                       "trading; the broker account stays as is.", sym)

    def blocked(self, now: datetime) -> dict[str, str]:
        """Symbols whose new shorts (or all orders) are held back right now."""
        out = {}
        for sym in self.managed():
            why = self._held_back(now, sym, opens_short=True)
            note = self.short_notes.get(sym)
            if not why and note and note[0] == now.astimezone(ET).date():
                why = note[1]
            if why:
                out[sym] = why
        return out

    def _submit(self, now: datetime, sym: str, delta: int, reason: str,
                order_type: str | None = None) -> dict | None:
        side = "buy" if delta > 0 else "sell"
        cid = f"td-{sym}-{uuid.uuid4().hex[:12]}"
        row = self.store.x(
            """INSERT INTO orders (ts, symbol, side, qty, reason, client_order_id, status)
               VALUES (?,?,?,?,?,?, 'submitting')""",
            (now.isoformat(), sym, side, abs(delta), reason, cid))
        kw = {"order_type": order_type} if order_type else {}
        if hasattr(self.broker, "quote"):  # IBKR: the model's last price as a fallback reference
            r = self.store.q("SELECT last_price FROM symbols WHERE symbol=? AND last_price IS NOT "
                             "NULL ORDER BY id DESC LIMIT 1", (sym,))
            if r:
                kw["ref_price"] = r[0]["last_price"]
        try:
            o = self.broker.submit(sym, abs(delta), side, cid, **kw)
        except BrokerError as e:
            self.store.x("UPDATE orders SET status='rejected', message=? WHERE id=?", (str(e)[:300], row))
            self.store.log(now, "broker-reject", f"{side} {abs(delta)} rejected: {e}", sym)
            return None
        self._record(row, o)
        self.store.log(now, "broker", f"{side.upper()} {abs(delta)} market ({reason}) -> {o.get('status')}", sym)
        return o

    def _await_fill(self, order: dict) -> bool:
        deadline = _time.monotonic() + self.fill_wait_s
        while True:
            o = self.broker.get_order(order["id"])
            row = self.store.q("SELECT id FROM orders WHERE broker_order_id=?", (order["id"],))
            if row:
                self._record(row[0]["id"], o)
            if o["status"] == "filled":
                return True
            if o["status"] in FINAL or _time.monotonic() >= deadline:
                return False
            _time.sleep(1)

    def _record(self, row_id: int, o: dict) -> None:
        self.store.x(
            """UPDATE orders SET broker_order_id=?, status=?, filled_qty=?, filled_avg_price=?,
               filled_at=? WHERE id=?""",
            (o.get("id"), o.get("status"), float(o.get("filled_qty") or 0),
             float(o["filled_avg_price"]) if o.get("filled_avg_price") else None,
             o.get("filled_at"), row_id))

    def _refresh_orders(self) -> None:
        final = ",".join(f"'{s}'" for s in FINAL)
        for r in self.store.q(f"SELECT id, broker_order_id FROM orders WHERE broker_order_id "
                              f"IS NOT NULL AND status NOT IN ({final})"):
            self._record(r["id"], self.broker.get_order(r["broker_order_id"]))

    # ---------------------------------------------------------------- read
    def status(self, max_age_s: float = 15.0) -> dict:
        if self.snapshot is None or _time.monotonic() - self.snapshot_at > max_age_s:
            try:
                acct = self.broker.account()
                pos = self.broker.positions()
                self.snapshot = {"account": acct, "positions": pos, "error": None}
            except (BrokerError, requests.RequestException) as e:
                self.snapshot = {"account": None, "positions": {}, "error": _msg(e)}
            self.snapshot_at = _time.monotonic()
        want = self.desired()
        pos = self.snapshot["positions"]
        held = self.blocked(self.last_sync or datetime.now(ET))
        recon = []
        for sym in sorted(self.managed() | set(want) | (set(pos) & self.managed())):
            p = pos.get(sym)
            paper = int(float(p["qty"])) if p else 0
            recon.append({
                "symbol": sym, "model": want.get(sym, 0), "paper": paper,
                "avg_entry": float(p["avg_entry_price"]) if p else None,
                "unrealized": float(p["unrealized_pl"]) if p else None,
                "match": paper == want.get(sym, 0),
                "note": held.get(sym),
                **self._recon_extra(sym),
            })
        acct = self.snapshot["account"]
        now = datetime.now(ET)
        start = self.day_start(now)
        day_pl = None
        if acct:
            if acct.get("last_equity") is not None:
                day_pl = float(acct["equity"]) - float(acct["last_equity"])
            elif start is not None:
                day_pl = float(acct["equity"]) - start
        stop = self.stop_reason(now)
        return {
            "venue": self.venue, "live": self.live,
            "control": {"kill": self._ctl("kill"), "stopped": stop[1] if stop else None,
                        "max_shares": self.max_shares, "daily_loss_limit": self.daily_loss_limit,
                        "day_start": start},
            "account": None if not acct else {
                "equity": float(acct["equity"]),
                "cash": None if acct.get("cash") is None else float(acct["cash"]),
                "buying_power": None if acct.get("buying_power") is None
                else float(acct["buying_power"]),
                "day_pl": day_pl, "account_id": acct.get("account_id"),
                "status": acct.get("status"), "shorting_enabled": acct.get("shorting_enabled"),
                "pattern_day_trader": acct.get("pattern_day_trader"),
                "trading_blocked": acct.get("trading_blocked"),
            },
            "error": self.snapshot["error"] or self.last_error,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "reconciliation": recon,
            "orders": self.store.q("SELECT * FROM orders ORDER BY id DESC LIMIT 200"),
            "curve": self.store.q("SELECT ts, equity FROM broker_equity ORDER BY ts"),
        }


class LiveSync(BrokerSync):
    """The IBKR account, fed by ONE book (live_book, IBKR_BOOK). In that book a
    watchlist symbol can be marked Live for the 5/10 EMA tranche only
    (`sleeves`; symbols.live). The account holds the sign of that tranche's
    position, capped at max_shares, and never above zero: short only, a long
    signal means flat. A cover after the model's stop goes out as a market
    order. Orders, events, the kill switch and the loss-limit latch live in
    this sync's own store."""

    def __init__(self, store, broker, books: dict, live_book: str = "5m",
                 sleeves: tuple = ("EMA5_10",), **kw):
        super().__init__(store, broker, **kw)
        self.books = books  # key -> (label, Store)
        self.live_book, self.sleeves = live_book, tuple(sleeves)
        self.book_label, self.book_store = books[live_book]

    def enabled(self) -> bool:
        return bool(self.store.settings()["broker_sync_enabled"])

    def selections(self) -> dict[str, dict]:
        """ticker -> {id, label, sleeves} for every live symbol of the live book.
        Other books and other tranches are ignored even if marked."""
        out = {}
        for r in self.book_store.q("SELECT id, symbol, live FROM symbols WHERE live IS NOT NULL "
                                   "AND status != 'removed' ORDER BY id"):
            sleeves = [x for x in r["live"].split(",") if x in self.sleeves]
            if sleeves:
                out.setdefault(r["symbol"], {"id": r["id"], "label": self.book_label,
                                             "sleeves": sleeves})
        return out

    def _net(self) -> dict[str, int]:
        want: dict[str, int] = {}
        for sym, sel in self.selections().items():
            marks = ",".join("?" * len(sel["sleeves"]))
            for t in self.book_store.q(f"SELECT side, qty FROM tranches WHERE status='open' "
                                       f"AND symbol_id=? AND sleeve IN ({marks})",
                                       (sel["id"], *sel["sleeves"])):
                want[sym] = want.get(sym, 0) + (t["qty"] if t["side"] == "long" else -t["qty"])
            want.setdefault(sym, 0)
        return want

    def desired(self) -> dict[str, int]:
        """Short only: never above zero; a long in the model means flat here."""
        return {s: min(q, 0) for s, q in super().desired().items()}

    def managed(self) -> set[str]:
        """Live symbols, plus anything this link ever traded: a symbol switched
        off (or removed) is closed, never left behind."""
        traded = {r["symbol"] for r in self.store.q("SELECT DISTINCT symbol FROM orders")}
        return set(self.selections()) | traded

    def _renamed(self) -> dict[str, str]:
        return {r["symbol"]: r["renamed_from"] for r in self.book_store.q(
            "SELECT symbol, renamed_from FROM symbols WHERE renamed_from IS NOT NULL "
            "AND live IS NOT NULL")}

    def _order_type(self, sym: str, target: int, cur: int) -> str | None:
        """Market for the cover of a short the model just stopped out: its most
        recent closed live tranche exited on the stop after this link's last fill."""
        if not (cur < 0 and target == 0):
            return None
        marks = ",".join("?" * len(self.sleeves))
        last = self.book_store.q(f"SELECT exit_time, exit_reason FROM tranches WHERE symbol=? AND "
                                 f"status='closed' AND sleeve IN ({marks}) ORDER BY exit_time DESC "
                                 f"LIMIT 1", (sym, *self.sleeves))
        if not last or not (last[0]["exit_reason"] or "").startswith("stop"):
            return None
        fill = self.store.q("SELECT ts FROM orders WHERE symbol=? AND status='filled' "
                            "ORDER BY id DESC LIMIT 1", (sym,))
        if fill and datetime.fromisoformat(fill[0]["ts"]) >= datetime.fromisoformat(last[0]["exit_time"]):
            return None
        return "market"

    def _recon_extra(self, sym: str) -> dict:
        sel = self.selections().get(sym)
        return {"book": sel["label"] if sel else None,
                "sleeves": sel["sleeves"] if sel else []}
