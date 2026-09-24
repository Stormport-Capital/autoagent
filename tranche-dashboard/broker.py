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
"""

from __future__ import annotations

import os
import threading
import time as _time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import requests

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
    def from_env(cls) -> "AlpacaPaper | None":
        key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
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


class BrokerSync:
    def __init__(self, store, broker, fill_wait_s: float = 20.0):
        self.store, self.broker = store, broker
        self.fill_wait_s = fill_wait_s
        self.lock = threading.Lock()
        self.last_sync: datetime | None = None
        self.last_error: str | None = None
        self.snapshot: dict | None = None      # cached account + positions for the UI
        self.snapshot_at = 0.0
        self.needs_followup = False            # an order was working: re-sync soon

    # ---------------------------------------------------------------- sync
    def desired(self) -> dict[str, int]:
        want: dict[str, int] = {}
        for t in self.store.q("SELECT symbol, side, qty FROM tranches WHERE status='open'"):
            want[t["symbol"]] = want.get(t["symbol"], 0) + (t["qty"] if t["side"] == "long" else -t["qty"])
        return want

    def managed(self) -> set[str]:
        return {r["symbol"] for r in self.store.q("SELECT DISTINCT symbol FROM symbols")}

    def sync(self, now: datetime) -> None:
        with self.lock:
            try:
                self._refresh_orders()
                want = self.desired()
                pos = {s: int(float(p["qty"])) for s, p in self.broker.positions().items()}
                busy = {o["symbol"] for o in self.broker.open_orders()}
                self.needs_followup = False
                for sym in sorted(self.managed() | set(want)):
                    target, cur = want.get(sym, 0), pos.get(sym, 0)
                    if target == cur:
                        continue
                    if sym in busy:
                        self.needs_followup = True
                        continue
                    if cur and target and (cur > 0) != (target > 0):
                        order = self._submit(now, sym, -cur, f"close {cur:+d} before reversing")
                        if not order or not self._await_fill(order):
                            self.needs_followup = order is not None
                            continue  # opening leg goes out on a follow-up sync
                        cur = 0
                    self._submit(now, sym, target - cur, f"model {target:+d}, paper {cur:+d}")
                acct = self.broker.account()
                self.store.x("INSERT OR REPLACE INTO broker_equity VALUES (?, ?)",
                             (now.isoformat(), float(acct["equity"])))
                self.last_error = None
            except (BrokerError, requests.RequestException) as e:
                self.last_error = _msg(e)
                self.store.log(now, "broker-error", self.last_error[:300])
            self.last_sync = now
            self.snapshot_at = 0.0  # force a fresh account read

    def _submit(self, now: datetime, sym: str, delta: int, reason: str) -> dict | None:
        side = "buy" if delta > 0 else "sell"
        cid = f"td-{sym}-{uuid.uuid4().hex[:12]}"
        row = self.store.x(
            """INSERT INTO orders (ts, symbol, side, qty, reason, client_order_id, status)
               VALUES (?,?,?,?,?,?, 'submitting')""",
            (now.isoformat(), sym, side, abs(delta), reason, cid))
        try:
            o = self.broker.submit(sym, abs(delta), side, cid)
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
        recon = []
        for sym in sorted(self.managed() | set(want) | (set(pos) & self.managed())):
            p = pos.get(sym)
            paper = int(float(p["qty"])) if p else 0
            recon.append({
                "symbol": sym, "model": want.get(sym, 0), "paper": paper,
                "avg_entry": float(p["avg_entry_price"]) if p else None,
                "unrealized": float(p["unrealized_pl"]) if p else None,
                "match": paper == want.get(sym, 0),
            })
        acct = self.snapshot["account"]
        return {
            "account": None if not acct else {
                "equity": float(acct["equity"]), "cash": float(acct["cash"]),
                "buying_power": float(acct["buying_power"]),
                "day_pl": float(acct["equity"]) - float(acct["last_equity"]),
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
