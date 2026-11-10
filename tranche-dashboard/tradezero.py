"""TradeZero link (paper or LIVE) for the books in TZ_BOOKS (default the
5-minute book only; for example TZ_BOOKS=5m,15m,1h), over TradeZero's REST
Trading API (https://webapi.tradezero.com/v1/api). Same scope as the IBKR
link: short only, the 5/10 EMA tranche only, flat by 15:55, one share.

TradeZeroBroker speaks the same small interface as ibkr.IBKRBroker (account,
positions, open_orders, get_order, submit, cancel, short_check, day_pnl), so
broker.LiveSync drives it exactly like the IBKR account. One live venue is
linked at a time (app.link_live).

Safety, enforced here independently of BrokerSync:
  * The key pair in .env decides the mode. TZ_MODE=paper needs an account
    TradeZero reports as accountType "Paper"; TZ_MODE=live needs any other
    type. The first call of every request checks this; a mismatch sends
    nothing. The account must also be "Active".
  * No order may take a symbol's position beyond TZ_MAX_SHARES (default 1)
    in either direction. Orders that only reduce a position are always allowed.
    TZ_SHARES=100 makes the position a fixed 100 shares whenever the model's
    tranche is short (and sets the cap to 100 unless TZ_MAX_SHARES says more):
    a locate covers 100 shares at minimum, so a 100-share short uses what the
    locate fee bought. The order-value cap (TZ_MAX_ORDER_USD) still applies to
    the whole order.
  * Short only: a buy that would leave any position above zero is refused.
  * No order worth more than TZ_MAX_ORDER_USD (default 1,000). TradeZero's
    API has no quotes, so the model's last price is the reference (passed in
    by BrokerSync). An OPENING order with no price is refused; a CLOSING
    order with no price goes out, because leaving a short open is the bigger
    risk at one share.
  * Market orders by default (TZ_ORDER_TYPE=market): the API has no bid/ask
    to build a marketable limit from, and every order of this link falls in
    regular hours (entries at bar checks, flat by 15:55), where TradeZero
    accepts Day market orders. TZ_ORDER_TYPE=limit uses the model's last
    price plus TZ_LIMIT_COLLAR_PCT instead, re-priced by BrokerSync after
    TZ_REPRICE_S seconds.
  * Borrow: GET is-easy-to-borrow before every opening short. Not easy to
    borrow on paper = skipped (paper has no locates). On live, a locate is
    bought only when TZ_LOCATE_MAX_PCT is above 0 and the offered fee per
    share is at or below that percent of the share price (the model's last
    price; no price = no locate), and today's locate spend (from TradeZero's
    own locate history) stays within TZ_LOCATE_MAX_USD_DAY. Lots of 100
    shares, the API's minimum.
  * Live orders need a route. TZ_ROUTE, or SMART when the account offers it,
    or the only route offered; anything else refuses. Paper routes itself.
  * One HTTP call at a time, spaced per TradeZero's published per-endpoint
    rate limits; a 429 waits 2 s, then 4 s, then gives up until the next check.
  * clientOrderId is BrokerSync's td-SYMBOL-hex id (never reused: TradeZero
    refuses a repeat forever, R114). It is also the order's id here, because
    the REST API has no other order key.
"""

from __future__ import annotations

import os
import re
import threading
import time as _time
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import requests

from broker import BrokerError
from ibkr import BOOK_KEYS, _num, limit_price
from indicators import ET

BASE_URL = "https://webapi.tradezero.com/v1/api"
HOST = "webapi.tradezero.com"
WORKING = {"PendingNew", "New", "PartiallyFilled"}
STATUS = {"PendingNew": "accepted", "New": "accepted", "PartiallyFilled": "partially_filled",
          "Filled": "filled", "Canceled": "canceled", "Rejected": "rejected",
          "Expired": "expired", "DoneForDay": "done_for_day"}
# seconds between two calls of the same kind (TradeZero's limits: orders 10/s,
# order reads 2/s, positions and accounts 3/s, P&L and borrow checks 1/s,
# locate quotes 1/s, locate history/inventory 2/s)
SPACING = {"accounts": 0.34, "pnl": 1.0, "positions": 0.34, "orders": 0.5, "order": 0.5,
           "post_order": 0.1, "cancel": 0.1, "routes": 1.0, "etb": 1.0,
           "loc_quote": 1.0, "loc_accept": 0.1, "loc_history": 0.5, "loc_inventory": 0.5}
RATE_WAITS = (2.0, 4.0)       # after a 429, before each retry
REGISTER_S = 5.0              # how long a just-sent order may take to show up on GET
RECENT_S = 30.0               # a 404 within this long of a submit is "not registered yet"
LOCATE_LOT = 100
LOC_OFFERED, LOC_FILLED, LOC_PENDING, LOC_REJECTED, LOC_EXPIRED = 65, 50, 54, 56, 67
LOCATE_COOLDOWN_S = 30.0      # TradeZero: one quote per symbol per 30 s


@dataclass
class TZConfig:
    account: str
    mode: str                  # paper | live
    key_id: str
    secret: str
    book: str = "5m"
    books: tuple = ("5m",)
    max_shares: int = 1
    shares: int | None = None  # TZ_SHARES: a fixed position size; None = the model's size, capped
    daily_loss_limit: float = 100.0
    max_order_usd: float = 1000.0
    order_type: str = "market"  # market | limit
    collar_pct: float = 3.0
    reprice_s: int = 45
    route: str | None = None    # live only: a route name from GET /routes
    locate_max_pct: float = 0.0  # locate fee cap, % of the share price; 0 = never buy one
    locate_max_usd_day: float = 5.0
    base_url: str = BASE_URL


def config_from_env(env=None) -> TZConfig | None:
    """None when TZ_ACCOUNT is unset. Raises BrokerError on anything
    inconsistent, so a half-written setup never links."""
    env = os.environ if env is None else env
    account = (env.get("TZ_ACCOUNT") or "").strip()
    if not account:
        return None
    key_id = (env.get("TZ_API_KEY_ID") or "").strip()
    secret = (env.get("TZ_API_SECRET_KEY") or "").strip()
    if not key_id or not secret:
        raise BrokerError("TZ_ACCOUNT is set but TZ_API_KEY_ID / TZ_API_SECRET_KEY are not "
                          "(the key pair from TradeZero's API Keys page)")
    mode = (env.get("TZ_MODE") or "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise BrokerError("TZ_MODE must be paper or live")
    raw = env.get("TZ_BOOKS") or "5m"
    books = tuple(dict.fromkeys(x for x in re.split(r"[\s,;]+", raw.strip().lower()) if x))
    bad = [x for x in books if x not in BOOK_KEYS]
    if bad or not books:
        raise BrokerError(f"TZ_BOOKS must list books from {', '.join(BOOK_KEYS)} "
                          f"(for example 5m,15m,1h); got {raw!r}")
    order_type = (env.get("TZ_ORDER_TYPE") or "market").strip().lower()
    if order_type not in ("limit", "market"):
        raise BrokerError("TZ_ORDER_TYPE must be market or limit")
    loss = _num(env, "TZ_DAILY_LOSS_LIMIT", 100.0, float, 0)
    if mode == "live" and loss <= 0:
        raise BrokerError("TZ_DAILY_LOSS_LIMIT must be above 0 for a live account")
    route = (env.get("TZ_ROUTE") or "").strip() or None
    shares = _num(env, "TZ_SHARES", 0, int, 0) or None
    max_shares = _num(env, "TZ_MAX_SHARES", shares or 1, int, 1)
    if shares and shares > max_shares:
        raise BrokerError(f"TZ_SHARES={shares} is above TZ_MAX_SHARES={max_shares}; raise the cap "
                          f"or drop it (TZ_SHARES alone sets both)")
    if (env.get("TZ_LOCATE_MAX_PER_SHARE") or "").strip():
        raise BrokerError("TZ_LOCATE_MAX_PER_SHARE was replaced by TZ_LOCATE_MAX_PCT, the locate "
                          "fee as a percent of the share price (0.5 = half a percent); "
                          "replace the line in .env")
    return TZConfig(
        account=account, mode=mode, key_id=key_id, secret=secret, book=books[0], books=books,
        max_shares=max_shares, shares=shares,
        daily_loss_limit=loss,
        max_order_usd=_num(env, "TZ_MAX_ORDER_USD", 1000.0, float, 1),
        order_type=order_type,
        collar_pct=_num(env, "TZ_LIMIT_COLLAR_PCT", 3.0, float, 0),
        reprice_s=_num(env, "TZ_REPRICE_S", 45, int, 0),
        route=route,
        locate_max_pct=_num(env, "TZ_LOCATE_MAX_PCT", 0.0, float, 0),
        locate_max_usd_day=_num(env, "TZ_LOCATE_MAX_USD_DAY", 5.0, float, 0),
    )


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _rows(body, key: str) -> list:
    """TradeZero wraps lists ({"positions": [...]}) except where it doesn't
    (paper locate inventory is a bare [])."""
    if isinstance(body, dict):
        v = body.get(key)
        return list(v) if isinstance(v, list) else []
    return list(body) if isinstance(body, list) else []


class TradeZeroBroker:
    name = "TradeZero"
    books_var = "TZ_BOOKS"
    uses_ref_price = True  # BrokerSync passes the model's last price to submit()

    def __init__(self, cfg: TZConfig, session=None, sleep=_time.sleep):
        if urlparse(cfg.base_url).hostname != HOST:
            raise BrokerError(f"refusing a TradeZero endpoint other than {HOST}")
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        self.live = cfg.mode == "live"
        self.venue = "TradeZero LIVE" if self.live else "TradeZero paper"
        self.endpoint = f"TradeZero API ({HOST}), account {cfg.account} ({cfg.mode})"
        self.max_shares, self.daily_loss_limit = cfg.max_shares, cfg.daily_loss_limit
        self.fixed_shares = cfg.shares  # BrokerSync.desired: exactly this many, or none
        self.reprice_s = cfg.reprice_s if cfg.order_type == "limit" else 0
        self.http = session or requests.Session()
        self.http.headers.update({"TZ-API-KEY-ID": cfg.key_id, "TZ-API-SECRET-KEY": cfg.secret,
                                  "Accept": "application/json"})
        self._sleep = sleep
        self._lock = threading.RLock()
        self._last: dict[str, float] = {}      # call kind -> monotonic time of the last one
        self._route: str | None = None
        self._sent: dict[str, float] = {}      # clientOrderId -> monotonic time it was sent
        self._pnl: tuple[float, dict] | None = None
        self._quoted: dict[str, float] = {}    # symbol -> monotonic time of the last locate quote
        self._notes: list[tuple[str, str]] = []  # (symbol, message) for the dashboard log

    # ------------------------------------------------------------ plumbing
    def _req(self, kind: str, method: str, path: str, json=None, data=None,
             ok404: bool = False):
        url = self.base + path
        if urlparse(url).hostname != HOST:  # belt and braces
            raise BrokerError(f"refusing a TradeZero endpoint other than {HOST}")
        with self._lock:
            gap = SPACING.get(kind, 0.5)
            last = self._last.get(kind)
            if last is not None and _time.monotonic() - last < gap:
                self._sleep(gap - (_time.monotonic() - last))
            for attempt in range(len(RATE_WAITS) + 1):
                self._last[kind] = _time.monotonic()
                r = self.http.request(method, url, timeout=20, json=json, data=data)
                if r.status_code != 429:
                    break
                if attempt < len(RATE_WAITS):
                    self._sleep(RATE_WAITS[attempt])
            else:
                raise BrokerError("TradeZero is rate-limiting this key (429); will retry on "
                                  "the next check")
        if r.status_code == 429:
            raise BrokerError("TradeZero is rate-limiting this key (429); will retry on the "
                              "next check")
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            raise BrokerError(f"TradeZero {r.status_code}: {self._error_text(r)}")
        ctype = (r.headers.get("Content-Type") or "").lower()
        if not r.content:
            return None
        if "json" in ctype:
            return r.json()
        try:
            return r.json()
        except ValueError:
            return r.text

    @staticmethod
    def _error_text(r) -> str:
        msg = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                msg = str(body.get("message") or body.get("detail") or body.get("text") or body)
        except ValueError:
            msg = (r.text or "").strip()
        msg = msg[:300]
        if r.status_code in (401, 404) and (not msg or msg.lower() in ("not found", "unauthorized")):
            msg = (f"{msg or 'refused'} - TradeZero answers like this when the API keys are "
                   f"wrong or the account isn't theirs; check TZ_API_KEY_ID, TZ_API_SECRET_KEY "
                   f"and TZ_ACCOUNT in .env")
        return msg

    def drain_notes(self) -> list[tuple[str, str]]:
        """(symbol, message) events since the last call, for BrokerSync to log."""
        out, self._notes = self._notes, []
        return out

    # ------------------------------------------------------------ account
    def _account_row(self) -> dict:
        """The configured account's row from GET /accounts, checked against
        TZ_MODE. Every path to an order goes through here first."""
        body = self._req("accounts", "GET", "/accounts")
        rows = _rows(body, "accounts")
        want = self.cfg.account
        mine = [a for a in rows if str(a.get("account") or a.get("accountId") or "") == want]
        if not mine:
            seen = ", ".join(str(a.get("account") or a.get("accountId") or "?") for a in rows)
            raise BrokerError(f"these TradeZero keys don't see account {want} (they see: "
                              f"{seen or 'no account'}); nothing sent")
        row = mine[0]
        typ = str(row.get("accountType") or "")
        paper = typ.strip().lower() == "paper"
        if self.cfg.mode == "paper" and not paper:
            raise BrokerError(f"TZ_MODE=paper but TradeZero says {want} is a "
                              f"{typ or 'non-paper'} account; nothing sent")
        if self.cfg.mode == "live" and paper:
            raise BrokerError(f"TZ_MODE=live but TradeZero says {want} is a Paper account; "
                              f"nothing sent")
        return row

    def _pnl_body(self, soft: bool = True) -> dict | None:
        """GET /pnl, cached for 2 s (it is read for equity and for day_pnl)."""
        if self._pnl and _time.monotonic() - self._pnl[0] < 2.0:
            return self._pnl[1]
        try:
            body = self._req("pnl", "GET", f"/accounts/{self.cfg.account}/pnl")
        except (BrokerError, requests.RequestException):
            if soft:
                return None
            raise
        if not isinstance(body, dict):
            return None
        self._pnl = (_time.monotonic(), body)
        return body

    def account(self) -> dict:
        row = self._account_row()
        pnl = self._pnl_body() or {}
        equity = _f(pnl.get("accountValue"))
        if equity is None:
            equity = _f(row.get("equity"))
        if equity is None:
            raise BrokerError("TradeZero sent no account value; will retry on the next check")
        status = row.get("accountStatus")
        return {"equity": equity,
                "cash": _f(row.get("availableCash")),
                "buying_power": _f(row.get("buyingPower") if row.get("buyingPower") is not None
                                   else row.get("bp")),
                "last_equity": None, "status": status, "shorting_enabled": None,
                "trading_blocked": status is not None and str(status) != "Active",
                "account_id": self.cfg.account, "mode": self.cfg.mode,
                "account_type": row.get("accountType")}

    def day_pnl(self) -> float | None:
        """TradeZero's own P&L for today (None until it answers)."""
        pnl = self._pnl_body() or {}
        return _f(pnl.get("dayPnl"))

    # ------------------------------------------------------------ positions
    def positions(self) -> dict[str, dict]:
        body = self._req("positions", "GET", f"/accounts/{self.cfg.account}/positions")
        lots = [p for p in _rows(body, "positions")
                if str(p.get("securityType") or "Stock") == "Stock" and p.get("symbol")]
        pnl = self._pnl_body() or {}
        unreal = {str(r.get("positionId")): _f(r.get("unrealizedPnL")) or 0.0
                  for r in _rows(pnl, "pnl") if r.get("positionId") is not None}
        out: dict[str, dict] = {}
        for p in lots:
            shares = int(round(_f(p.get("shares")) or 0))
            if not shares:
                continue
            q = -shares if str(p.get("side") or "").lower() == "short" else shares
            sym = str(p["symbol"]).upper()
            cur = out.setdefault(sym, {"qty": 0, "cost": 0.0, "shares": 0, "unreal": 0.0})
            cur["qty"] += q
            cur["cost"] += abs(shares) * (_f(p.get("priceAvg")) or 0.0)
            cur["shares"] += abs(shares)
            cur["unreal"] += unreal.get(str(p.get("positionId")), 0.0)
        return {s: {"qty": str(c["qty"]),
                    "avg_entry_price": str(round(c["cost"] / c["shares"], 4)) if c["shares"] else "0",
                    "unrealized_pl": str(round(c["unreal"], 2))}
                for s, c in out.items() if c["qty"]}

    def _position(self, symbol: str) -> int:
        p = self.positions().get(symbol.upper())
        return int(float(p["qty"])) if p else 0

    # ------------------------------------------------------------ orders
    @staticmethod
    def _order(o: dict) -> dict:
        st = str(o.get("orderStatus") or "")
        executed = _f(o.get("executed")) or 0.0
        status = STATUS.get(st) or ("partially_filled" if executed else "accepted")
        avg = _f(o.get("priceAvg"))
        cid = o.get("clientOrderId")
        return {"id": cid, "client_order_id": cid, "symbol": o.get("symbol"),
                "status": status, "tz_status": st, "filled_qty": str(executed),
                "filled_avg_price": str(avg) if executed and avg else None,
                "filled_at": o.get("lastUpdated") if status == "filled" else None,
                "message": o.get("text") or None}

    def open_orders(self) -> list[dict]:
        body = self._req("orders", "GET", f"/accounts/{self.cfg.account}/orders")
        return [self._order(o) for o in _rows(body, "orders")
                if str(o.get("orderStatus") or "") in WORKING
                and str(o.get("securityType") or "Stock") == "Stock"]

    def get_order(self, order_id: str) -> dict:
        body = self._req("order", "GET", f"/accounts/{self.cfg.account}/order/{order_id}",
                         ok404=True)
        if isinstance(body, dict) and body.get("clientOrderId"):
            return self._order(body)
        sent = self._sent.get(order_id)
        if sent is not None and _time.monotonic() - sent < RECENT_S:
            return {"id": order_id, "client_order_id": order_id, "status": "accepted",
                    "tz_status": "PendingNew", "filled_qty": "0", "filled_avg_price": None,
                    "filled_at": None, "message": "TradeZero hasn't registered it yet"}
        return {"id": order_id, "client_order_id": order_id, "status": "expired",
                "tz_status": "", "filled_qty": "0", "filled_avg_price": None, "filled_at": None,
                "message": "not known to TradeZero (an earlier session?); check positions"}

    def _route_name(self) -> str | None:
        """Live orders must name a route the account offers; paper routes itself."""
        if not self.live:
            return None
        if self._route:
            return self._route
        body = self._req("routes", "GET", f"/accounts/{self.cfg.account}/routes")
        names = [str(r.get("routeName")) for r in _rows(body, "routes") if r.get("routeName")]
        want = self.cfg.route or "SMART"
        if want in names:
            self._route = want
        elif self.cfg.route is None and len(names) == 1:
            self._route = names[0]
        else:
            raise BrokerError(f"TradeZero offers route(s) {', '.join(names) or 'none'} on "
                              f"{self.cfg.account}, not {want}; set TZ_ROUTE to one of them")
        return self._route

    def submit(self, symbol: str, qty: int, side: str, client_order_id: str,
               ref_price: float | None = None, order_type: str | None = None) -> dict:
        cfg = self.cfg
        qty = int(qty)
        if qty < 1 or side not in ("buy", "sell"):
            raise BrokerError(f"TradeZero: bad order {side} {qty}")
        if not client_order_id or len(client_order_id) > 36:
            raise BrokerError("TradeZero: clientOrderId must be 1-36 characters")
        row = self._account_row()
        status = str(row.get("accountStatus") or "")
        if status and status != "Active":
            raise BrokerError(f"TradeZero account {cfg.account} is {status}, not Active; "
                              f"nothing sent")
        symbol = symbol.upper()
        cur = self._position(symbol)
        new = cur + (qty if side == "buy" else -qty)
        if abs(new) > cfg.max_shares and abs(new) > abs(cur):
            raise BrokerError(f"TradeZero: refused, {symbol} would go from {cur:+d} to {new:+d} "
                              f"shares; the cap is {cfg.max_shares} (TZ_MAX_SHARES)")
        if new > 0:
            raise BrokerError(f"TradeZero: refused, a buy would take {symbol} from {cur:+d} to "
                              f"{new:+d}; this link is short only")
        closing = (cur < 0 and side == "buy") or (cur > 0 and side == "sell")
        if closing and abs(qty) > abs(cur):
            raise BrokerError(f"TradeZero: refused, {side} {qty} {symbol} would cross through "
                              f"zero from {cur:+d}; close it first")
        px = _f(ref_price)
        if px is not None and px <= 0:
            px = None
        if px is not None and px * qty > cfg.max_order_usd:
            raise BrokerError(f"TradeZero: refused, order value ${px * qty:,.2f} is above "
                              f"TZ_MAX_ORDER_USD ${cfg.max_order_usd:,.0f}")
        if px is None and not closing:
            raise BrokerError(f"TradeZero: no reference price for {symbol} to check the order "
                              f"against (the model has no last price yet); not sent")
        kind = (order_type or cfg.order_type).lower()
        body = {"securityType": "Stock", "symbol": symbol,
                "side": "Buy" if side == "buy" else "Sell",
                "openClose": "Close" if closing else "Open",
                "orderType": "Market", "orderQuantity": qty, "timeInForce": "Day",
                "clientOrderId": client_order_id}
        if kind == "limit" and px is not None:
            body["orderType"] = "Limit"
            body["limitPrice"] = limit_price(side, px, cfg.collar_pct)
        route = self._route_name()
        if route:
            body["route"] = route
        sent = datetime.now(ET).isoformat()  # bar close to here is the latency that matters
        self._sent[client_order_id] = _time.monotonic()
        while len(self._sent) > 500:
            del self._sent[next(iter(self._sent))]
        resp = self._req("post_order", "POST", f"/accounts/{cfg.account}/order", json=body)
        if not isinstance(resp, dict):
            raise BrokerError(f"TradeZero: unexpected answer to the order: {str(resp)[:120]}")
        o = self._order(resp)
        o["id"] = o["client_order_id"] = client_order_id  # a rejection may echo a changed id
        if o["status"] == "rejected":
            err = BrokerError(f"TradeZero: {o['message'] or 'order Rejected'}")
            err.sent_at = sent  # it did reach TradeZero
            raise err
        for _ in range(int(REGISTER_S / 0.5)):  # until registered and past PendingNew
            if o["tz_status"] not in ("", "PendingNew"):
                break
            self._sleep(0.5)
            got = self._req("order", "GET", f"/accounts/{cfg.account}/order/{client_order_id}",
                            ok404=True)
            if isinstance(got, dict) and got.get("clientOrderId"):
                o = self._order(got)
        if o["status"] == "rejected" and not float(o["filled_qty"]):
            err = BrokerError(f"TradeZero: {o['message'] or 'order Rejected'}")
            err.sent_at = sent
            raise err
        o["sent_at"] = sent
        return o

    def cancel(self, order_id: str) -> None:
        o = self.get_order(order_id)
        if o["status"] not in ("accepted", "partially_filled"):
            return  # never cancel a Rejected order (TradeZero overwrites its reason with R130)
        self._req("cancel", "DELETE", f"/accounts/{self.cfg.account}/orders/{order_id}",
                  ok404=True)
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline:
            self._sleep(0.5)
            if self.get_order(order_id)["status"] not in ("accepted", "partially_filled"):
                return

    # ------------------------------------------------------------ borrow
    def easy_to_borrow(self, symbol: str) -> bool | None:
        body = self._req("etb", "GET", f"/accounts/{self.cfg.account}/is-easy-to-borrow/symbol/"
                                       f"{symbol.upper()}")
        if isinstance(body, dict) and "isEasyToBorrow" in body:
            v = body["isEasyToBorrow"]
            return v if isinstance(v, bool) else str(v).lower() == "true"
        return None

    def inventory(self, symbol: str) -> int:
        """Located shares available to short in `symbol` (live accounts)."""
        body = self._req("loc_inventory", "GET", f"/accounts/{self.cfg.account}/locates/inventory")
        return sum(int(_f(r.get("available")) or 0) for r in _rows(body, "locateInventory")
                   if str(r.get("symbol") or "").upper() == symbol.upper())

    def locate_history(self) -> list[dict]:
        body = self._req("loc_history", "GET", f"/accounts/{self.cfg.account}/locates/history")
        return _rows(body, "locateHistory")

    def locate_spend_today(self, rows: list[dict] | None = None) -> float:
        """Dollars paid for locates today, from TradeZero's own history
        (filled rows: price per share x shares filled)."""
        rows = self.locate_history() if rows is None else rows
        return sum((_f(r.get("locatePrice")) or 0.0) * (_f(r.get("filledShares")) or 0.0)
                   for r in rows if r.get("locateStatus") == LOC_FILLED
                   and (_f(r.get("locateShares")) or 0) > 0)

    def short_check(self, symbol: str, qty: int, ref_price: float | None = None) -> str | None:
        """Why a new short of `qty` can't be opened (None = go ahead). Easy to
        borrow: go. Otherwise paper skips (it has no locates) and live buys a
        locate within the caps, or says why not. `ref_price` is the model's
        last price, which the fee cap is a percent of."""
        symbol = symbol.upper()
        self._account_row()
        etb = self.easy_to_borrow(symbol)
        if etb is None or etb:
            return None  # no answer: TradeZero's own check at order time decides
        if not self.live:
            return "TradeZero lists it as not easy to borrow (paper accounts can't locate)"
        if self.inventory(symbol) >= qty:
            return None
        if self.cfg.locate_max_pct <= 0:
            return ("TradeZero lists it as not easy to borrow and locates are off "
                    "(TZ_LOCATE_MAX_PCT)")
        return self._locate(symbol, qty, ref_price)

    def _locate(self, symbol: str, qty: int, ref_price: float | None) -> str | None:
        cfg = self.cfg
        px = _f(ref_price)
        if px is None or px <= 0:
            return (f"a locate for {symbol} would be needed, but the model has no price for it "
                    f"yet to judge the fee against; not bought")
        last = self._quoted.get(symbol)
        if last is not None and _time.monotonic() - last < LOCATE_COOLDOWN_S:
            return (f"a locate quote for {symbol} went out {_time.monotonic() - last:.0f}s ago; "
                    f"TradeZero allows one per 30 s - next check")
        lot = max(LOCATE_LOT, -(-qty // LOCATE_LOT) * LOCATE_LOT)
        spent = self.locate_spend_today()
        qid = f"td-{symbol}-{uuid.uuid4().hex[:10]}"
        self._quoted[symbol] = _time.monotonic()
        self._req("loc_quote", "POST", "/accounts/locates/quote",
                  json={"account": cfg.account, "symbol": symbol, "quantity": lot,
                        "quoteReqID": qid})
        mine: list[dict] = []
        offered: list[dict] = []
        for _ in range(5):  # TradeZero: wait 2 s, then poll up to four times, 2 s apart
            self._sleep(2.0)
            mine = [r for r in self.locate_history()
                    if str(r.get("quoteReqID") or "") in (qid, qid + ".SU")]
            offered = [r for r in mine if r.get("locateStatus") == LOC_OFFERED
                       and (_f(r.get("locatePrice")) or 0) > 0]
            if offered:
                break
            if mine and all(r.get("locateStatus") in (LOC_REJECTED, LOC_EXPIRED) for r in mine):
                break
        if not offered:
            why = next((str(r.get("text")) for r in mine if r.get("text")), "")
            return f"TradeZero offered no shares of {symbol} to borrow" + (f" ({why[:80]})" if why else "")
        best = min(offered, key=lambda r: _f(r.get("locatePrice")) or 0)
        price = _f(best.get("locatePrice")) or 0.0
        cost = price * lot
        pct = price / px * 100
        if pct > cfg.locate_max_pct:
            return (f"a locate for {symbol} costs ${price:.4f}/share = {pct:.2f}% of the "
                    f"${px:.2f} share price, above TZ_LOCATE_MAX_PCT {cfg.locate_max_pct:.2f}% "
                    f"(${cost:.2f} for {lot}); not bought")
        if spent + cost > cfg.locate_max_usd_day:
            return (f"a locate for {symbol} (${cost:.2f}) would take today's locate spend from "
                    f"${spent:.2f} past TZ_LOCATE_MAX_USD_DAY ${cfg.locate_max_usd_day:.2f}; "
                    f"not bought")
        self._req("loc_accept", "POST", "/accounts/locates/accept",
                  json={"accountId": cfg.account, "quoteReqID": best.get("quoteReqID")})
        kind = {3: "pre-borrow", 4: "single-use"}.get(best.get("locateType"), "locate")
        self._notes.append((symbol, f"bought a {kind} for {lot} {symbol} at ${price:.4f}/share "
                                    f"({pct:.2f}% of the ${px:.2f} price; ${cost:.2f}); today's "
                                    f"locate spend ${spent + cost:.2f}"))
        for _ in range(6):
            self._sleep(1.0)
            if self.inventory(symbol) >= qty:
                return None
        return (f"the {symbol} locate was accepted (${cost:.2f}) but the shares haven't shown "
                f"in the inventory yet; retrying at the next check")
