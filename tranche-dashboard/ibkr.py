"""Interactive Brokers link (paper or LIVE) for ONE book (IBKR_BOOK, default the
5-minute book), through IB Gateway.

Scope: short only, the 5/10 EMA tranche only, flat by 15:55, one share. Which
symbols trade is chosen per watchlist row in that book (Live button);
broker.LiveSync turns those choices into one target position per ticker.

IBKRBroker speaks the same small interface as broker.AlpacaPaper (account,
positions, open_orders, submit, get_order) plus cancel / quote / short_check /
day_pnl, so BrokerSync drives it exactly like the Alpaca paper account.

Safety, enforced here independently of BrokerSync:
  * The account in .env decides the mode: IBKR_MODE=paper needs a paper
    account ID (DU...), IBKR_MODE=live needs a live one (U...). The gateway
    must be logged in to that exact account, or nothing is sent.
  * No order may take a symbol's position beyond IBKR_MAX_SHARES (default 1)
    in either direction. Orders that only reduce a position are always allowed.
  * Short only: a buy that would leave any position above zero (a long) is
    refused. Covering a short is always allowed.
  * No order worth more than IBKR_MAX_ORDER_USD (default 1,000). An order with
    no price to check against is refused.
  * Marketable limit orders by default: the ask (buy) or bid (sell) from IBKR,
    plus IBKR_LIMIT_COLLAR_PCT (default 3%). BrokerSync cancels one that hasn't
    filled after IBKR_REPRICE_S seconds (default 45) and re-prices it on the
    next follow-up sync. A stop cover is sent as a market order.
  * Regular hours only (outsideRth off). Day orders.

ib_async is asyncio based, so all calls run on one private event-loop thread.
The import is lazy: without ib_async installed the dashboard still runs, and
only the IBKR link reports that it is missing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import os
import threading
import time as _time
from dataclasses import dataclass

from broker import BrokerError

DEFAULT_PORTS = {"paper": 4002, "live": 4001}  # IB Gateway; TWS uses 7497 / 7496
DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
STATUS = {"Filled": "filled", "Cancelled": "canceled", "ApiCancelled": "canceled",
          "Inactive": "rejected"}
WARNING_CODES = {0, 399, 2104, 2106, 2107, 2108, 2158, 10167}  # informational, not refusals


@dataclass
class IBKRConfig:
    account: str
    mode: str                  # paper | live
    book: str = "5m"           # the one book whose Live symbols trade here
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 17
    max_shares: int = 1
    daily_loss_limit: float = 100.0
    max_order_usd: float = 1000.0
    order_type: str = "limit"  # limit | market
    collar_pct: float = 3.0
    reprice_s: int = 45
    market_data_type: int = 1  # 1 real-time, 3 delayed (when there is no subscription)
    quote_wait_s: float = 3.0


def _num(env, key, default, cast, low=None):
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise BrokerError(f"{key}={raw!r} is not a number")
    if low is not None and v < low:
        raise BrokerError(f"{key} must be at least {low}")
    return v


def config_from_env(env=None) -> IBKRConfig | None:
    """None when IBKR_ACCOUNT is unset. Raises BrokerError on anything
    inconsistent, so a half-written live setup never connects."""
    env = os.environ if env is None else env
    account = (env.get("IBKR_ACCOUNT") or "").strip().upper()
    if not account:
        return None
    mode = (env.get("IBKR_MODE") or "paper").strip().lower()
    if mode not in DEFAULT_PORTS:
        raise BrokerError("IBKR_MODE must be paper or live")
    if mode == "paper" and not account.startswith("DU"):
        raise BrokerError(f"IBKR_MODE=paper but {account} is not a paper account ID (DU...)")
    if mode == "live" and not (account.startswith("U") and account[1:].isdigit()):
        raise BrokerError(f"IBKR_MODE=live but {account} is not a live account ID (U...)")
    book = (env.get("IBKR_BOOK") or "5m").strip().lower()
    if book not in ("1h", "15m", "5m"):
        raise BrokerError("IBKR_BOOK must be 1h, 15m or 5m")
    order_type = (env.get("IBKR_ORDER_TYPE") or "limit").strip().lower()
    if order_type not in ("limit", "market"):
        raise BrokerError("IBKR_ORDER_TYPE must be limit or market")
    loss = _num(env, "IBKR_DAILY_LOSS_LIMIT", 100.0, float, 0)
    if mode == "live" and loss <= 0:
        raise BrokerError("IBKR_DAILY_LOSS_LIMIT must be above 0 for a live account")
    return IBKRConfig(
        account=account, mode=mode, book=book,
        host=(env.get("IBKR_HOST") or "127.0.0.1").strip(),
        port=_num(env, "IBKR_PORT", DEFAULT_PORTS[mode], int, 1),
        client_id=_num(env, "IBKR_CLIENT_ID", 17, int, 0),
        max_shares=_num(env, "IBKR_MAX_SHARES", 1, int, 1),
        daily_loss_limit=loss,
        max_order_usd=_num(env, "IBKR_MAX_ORDER_USD", 1000.0, float, 1),
        order_type=order_type,
        collar_pct=_num(env, "IBKR_LIMIT_COLLAR_PCT", 3.0, float, 0),
        reprice_s=_num(env, "IBKR_REPRICE_S", 45, int, 0),
        market_data_type=_num(env, "IBKR_MARKET_DATA_TYPE", 1, int, 1),
    )


def _clean(v) -> float | None:
    """IB uses NaN for 'no data' and -1 for 'no bid/ask'."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or v <= 0 else v


def limit_price(side: str, px: float, collar_pct: float) -> float:
    """Marketable limit: through the quote by the collar, on a valid tick
    (cents from $1, 4 decimals below), rounded away from the market."""
    raw = px * (1 + collar_pct / 100) if side == "buy" else px * (1 - collar_pct / 100)
    step = 0.01 if px >= 1 else 0.0001
    n = raw / step
    n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
    return round(max(n, 1) * step, 4)


class IBKRBroker:
    def __init__(self, cfg: IBKRConfig, ib=None, api=None, connect_timeout: float = 15.0):
        if api is None:
            try:
                import ib_async as api  # noqa: N813 - lazy, optional dependency
            except ImportError:
                raise BrokerError("the IBKR link needs ib_async: pip install -r requirements.txt")
        self.cfg, self.api = cfg, api
        self.live = cfg.mode == "live"
        self.venue = "IBKR LIVE" if self.live else "IBKR paper"
        self.endpoint = f"IB Gateway {cfg.host}:{cfg.port}, account {cfg.account} ({cfg.mode})"
        self.max_shares, self.daily_loss_limit = cfg.max_shares, cfg.daily_loss_limit
        self.reprice_s = cfg.reprice_s if cfg.order_type == "limit" else 0
        self.connect_timeout = connect_timeout
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="ibkr-loop").start()
        self.ib = ib if ib is not None else self._run(api.IB)
        self._contracts: dict = {}
        self._next_try = 0.0
        self._pnl_on = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------ plumbing
    def _run(self, fn, timeout: float = 30.0):
        """Run fn() on the IB event loop (awaiting it if it is a coroutine)."""
        async def go():
            r = fn()
            if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
                r = await r
            return r
        fut = asyncio.run_coroutine_threadsafe(go(), self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise BrokerError("IB Gateway did not answer in time; will retry on the next check")

    def _ensure(self) -> None:
        with self._lock:
            if self._run(self.ib.isConnected):
                return
            if _time.monotonic() < self._next_try:
                raise BrokerError("not connected to IB Gateway; retrying shortly")
            self._pnl_on = False
            c = self.cfg
            try:
                self._run(lambda: self.ib.connectAsync(c.host, c.port, clientId=c.client_id,
                                                       timeout=self.connect_timeout,
                                                       account=c.account),
                          timeout=self.connect_timeout * 4)
            except BrokerError:
                self._next_try = _time.monotonic() + 30
                raise
            except Exception as e:
                self._next_try = _time.monotonic() + 30
                raise BrokerError(f"can't connect to IB Gateway at {c.host}:{c.port} "
                                  f"({type(e).__name__}); is it running and logged in?")
            accounts = [a.upper() for a in self._run(self.ib.managedAccounts)]
            if c.account not in accounts:
                self._run(self.ib.disconnect)
                self._next_try = _time.monotonic() + 30
                raise BrokerError(f"IB Gateway is logged in to {', '.join(accounts) or 'no account'}, "
                                  f"not {c.account}; nothing sent")
            self._run(lambda: self.ib.reqMarketDataType(c.market_data_type))

    def _contract(self, symbol: str):
        c = self._contracts.get(symbol)
        if c is None:
            stock = self.api.Stock(symbol.replace(".", " "), "SMART", "USD")
            got = self._run(lambda: self.ib.qualifyContractsAsync(stock))
            if not got or got[0] is None:
                raise BrokerError(f"IBKR: {symbol} not found as a US stock")
            c = self._contracts[symbol] = got[0]
        return c

    @staticmethod
    def _sym(contract) -> str:
        return contract.symbol.replace(" ", ".")

    def _order(self, t) -> dict:
        st = t.orderStatus
        filled = float(st.filled or 0)
        status = STATUS.get(st.status) or ("partially_filled" if filled else "accepted")
        msgs = [e.message for e in t.log if e.message and e.errorCode not in WARNING_CODES]
        fill_times = [f.time for f in t.fills if getattr(f, "time", None)]
        return {
            "id": str(t.order.orderId), "client_order_id": t.order.orderRef,
            "symbol": self._sym(t.contract), "status": status, "ib_status": st.status,
            "filled_qty": str(filled),
            "filled_avg_price": str(st.avgFillPrice) if filled and st.avgFillPrice else None,
            "filled_at": fill_times[-1].isoformat() if fill_times else None,
            "message": msgs[-1] if msgs else None,
        }

    def _trade(self, order_id: str):
        for t in self._run(self.ib.trades):
            if str(t.order.orderId) == str(order_id):
                return t
        return None

    def _position(self, symbol: str) -> int:
        for p in self._run(lambda: self.ib.positions(self.cfg.account)):
            if p.contract.secType == "STK" and self._sym(p.contract) == symbol:
                return int(p.position)
        return 0

    # ------------------------------------------------------------ interface
    def account(self) -> dict:
        self._ensure()
        vals = {}
        for v in self._run(lambda: self.ib.accountValues(self.cfg.account)):
            if v.currency in ("USD", "BASE"):
                vals.setdefault(v.tag, v.value)
                if v.currency == "USD":
                    vals[v.tag] = v.value

        def f(tag):
            try:
                return float(vals[tag])
            except (KeyError, ValueError):
                return None
        if f("NetLiquidation") is None:
            raise BrokerError("IBKR account values not loaded yet; will retry on the next check")
        return {"equity": f("NetLiquidation"), "cash": f("TotalCashValue"),
                "buying_power": f("BuyingPower"), "last_equity": None, "status": "ACTIVE",
                "shorting_enabled": None, "account_id": self.cfg.account, "mode": self.cfg.mode}

    def positions(self) -> dict[str, dict]:
        self._ensure()
        acct = self.cfg.account
        port = {self._sym(i.contract): i for i in self._run(lambda: self.ib.portfolio(acct))
                if i.contract.secType == "STK"}
        out = {}
        for p in self._run(lambda: self.ib.positions(acct)):
            if p.contract.secType != "STK" or not p.position:
                continue
            sym = self._sym(p.contract)
            item = port.get(sym)
            out[sym] = {"qty": str(int(p.position)), "avg_entry_price": str(p.avgCost),
                        "unrealized_pl": str(item.unrealizedPNL if item else 0)}
        return out

    def open_orders(self) -> list[dict]:
        self._ensure()
        return [self._order(t) for t in self._run(self.ib.openTrades)
                if t.order.account in (self.cfg.account, "") and t.orderStatus.status not in DONE]

    def get_order(self, order_id: str) -> dict:
        self._ensure()
        t = self._trade(order_id)
        if t is None:  # from an earlier gateway session and no longer open
            return {"id": str(order_id), "status": "expired", "filled_qty": "0",
                    "filled_avg_price": None, "filled_at": None,
                    "message": "not in this IB Gateway session; check positions"}
        return self._run(lambda: self._order(t))

    def quote(self, symbol: str) -> dict:
        """Bid/ask/last plus IBKR's short availability (generic tick 236):
        shortable > 2.5 = easy to borrow, > 1.5 = locate needed, else none."""
        self._ensure()
        c = self._contract(symbol)
        tk = self._run(lambda: self.ib.reqMktData(c, "236", False, False))

        def read():
            return {"bid": _clean(tk.bid), "ask": _clean(tk.ask), "last": _clean(tk.last),
                    "close": _clean(tk.close),
                    "shortable_shares": None if tk.shortableShares is None
                    or math.isnan(tk.shortableShares) else float(tk.shortableShares),
                    "shortable": None if tk.shortable is None or math.isnan(tk.shortable)
                    else float(tk.shortable)}
        deadline = _time.monotonic() + self.cfg.quote_wait_s
        try:
            while True:
                q = self._run(read)
                if (q["bid"] and q["ask"] and q["shortable"] is not None) \
                        or _time.monotonic() >= deadline:
                    return q
                _time.sleep(0.25)
        finally:
            self._run(lambda: self.ib.cancelMktData(c))

    def short_check(self, symbol: str, qty: int) -> str | None:
        """Why a new short of `qty` can't be opened (None = go ahead). When IBKR
        sends no short data, the order goes out and IBKR's own locate check
        accepts or rejects it."""
        q = self.quote(symbol)
        shares, level = q["shortable_shares"], q["shortable"]
        if level is not None and level <= 1.5:
            return "IBKR has no shares to borrow right now"
        if shares is not None and shares < qty:
            return f"IBKR has {shares:,.0f} shares to borrow, needs {qty}"
        return None

    def day_pnl(self) -> float | None:
        """IBKR's own daily P&L for the account (None until it arrives)."""
        self._ensure()
        acct = self.cfg.account
        if not self._pnl_on:
            self._run(lambda: self.ib.reqPnL(acct))
            self._pnl_on = True
        for p in self._run(lambda: self.ib.pnl(acct)):
            v = p.dailyPnL
            if v is not None and not math.isnan(v):
                return float(v)
        return None

    def submit(self, symbol: str, qty: int, side: str, client_order_id: str,
               ref_price: float | None = None, order_type: str | None = None) -> dict:
        self._ensure()
        cfg = self.cfg
        qty = int(qty)
        if qty < 1 or side not in ("buy", "sell"):
            raise BrokerError(f"IBKR: bad order {side} {qty}")
        cur = self._position(symbol)
        new = cur + (qty if side == "buy" else -qty)
        if abs(new) > cfg.max_shares and abs(new) > abs(cur):
            raise BrokerError(f"IBKR: refused, {symbol} would go from {cur:+d} to {new:+d} "
                              f"shares; the cap is {cfg.max_shares} (IBKR_MAX_SHARES)")
        if new > 0:
            raise BrokerError(f"IBKR: refused, a buy would take {symbol} from {cur:+d} to "
                              f"{new:+d}; this link is short only")
        c = self._contract(symbol)
        q = self.quote(symbol)
        px = (q["ask"] if side == "buy" else q["bid"]) or q["last"] or ref_price or q["close"]
        if not px:
            raise BrokerError(f"IBKR: no price for {symbol} to check the order against; not sent")
        if px * qty > cfg.max_order_usd:
            raise BrokerError(f"IBKR: refused, order value ${px * qty:,.2f} is above "
                              f"IBKR_MAX_ORDER_USD ${cfg.max_order_usd:,.0f}")
        action = "BUY" if side == "buy" else "SELL"
        if (order_type or cfg.order_type) == "market":
            order = self.api.MarketOrder(action, qty)
        else:
            order = self.api.LimitOrder(action, qty, limit_price(side, px, cfg.collar_pct))
        order.tif, order.outsideRth = "DAY", False
        order.orderRef, order.account = client_order_id, cfg.account
        trade = self._run(lambda: self.ib.placeOrder(c, order))
        deadline = _time.monotonic() + 5
        while True:  # wait for IBKR to accept or refuse it
            st = self._run(lambda: trade.orderStatus.status)
            if st in DONE or st in ("Submitted", "PreSubmitted") or _time.monotonic() >= deadline:
                break
            _time.sleep(0.2)
        o = self._run(lambda: self._order(trade))
        if o["status"] in ("rejected", "canceled") and not float(o["filled_qty"]):
            raise BrokerError(f"IBKR: {o['message'] or 'order ' + o['ib_status']}")
        return o

    def cancel(self, order_id: str) -> None:
        self._ensure()
        t = self._trade(order_id)
        if t is None or t.orderStatus.status in DONE:
            return
        self._run(lambda: self.ib.cancelOrder(t.order))
        deadline = _time.monotonic() + 5
        while self._run(lambda: t.orderStatus.status) not in DONE \
                and _time.monotonic() < deadline:
            _time.sleep(0.2)
