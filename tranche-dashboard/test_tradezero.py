"""Offline tests for the TradeZero link (tradezero.py): a fake of TradeZero's
REST API answers every request in memory. Run: python test_tradezero.py"""
import json
import os
import tempfile
import unittest
from datetime import datetime

import app
import tradezero
from broker import BrokerError, BrokerSync, LiveSync
from engine import LIVE_SLEEVES, Engine
from indicators import ET
from store import Store
from test_tranche import ScriptedProvider
from tradezero import TradeZeroBroker


class R:
    def __init__(self, status, body=None, text=None, ctype="application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": ctype}
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeTZ:
    """TradeZero's REST API in memory, as documented: one account, positions
    as lots, orders that fill at once unless `hold`, symbols in `reject`
    refused with HTTP 200 + orderStatus Rejected, an easy-to-borrow map, and
    the locate flow (quote -> history row Offered after `offer_after` polls
    -> accept -> inventory). `lag` GETs of a fresh order answer 404 first,
    as the real API can right after a POST."""

    REJECT = "R62: Short sale not permitted - not easy to borrow"

    def __init__(self, account="TZP1", acct_type="Paper", status="Active", equity=50000.0,
                 pos=None, lots=None, hold=False, reject=(), reject_text=REJECT, etb=None,
                 routes=("SMART", "ARCA"), locate_price=0.01, offer_after=1, no_inventory=(),
                 lag=0, rate429=0, inventory=None, auth_ok=True, day_pnl=-3.25):
        self.account, self.acct_type, self.status, self.equity = account, acct_type, status, equity
        self.pos = dict(pos or {})
        self.lots = lots  # explicit raw position rows, if given
        self.hold, self.reject, self.reject_text = hold, set(reject), reject_text
        self.etb = dict(etb or {})
        self.routes = list(routes)
        self.locate_price, self.offer_after, self.no_inventory = locate_price, offer_after, set(no_inventory)
        self.lag, self.rate429, self.auth_ok, self.day_pnl = lag, rate429, auth_ok, day_pnl
        self.inventory = dict(inventory or {})
        self.headers = {}  # requests.Session.headers stand-in: the key pair lands here
        self.calls = []  # (method, path, json body)
        self.orders = {}  # clientOrderId -> raw row
        self.gets_left = {}
        self.history = []
        self.polls = {}

    # ---- helpers
    def _acct(self):
        return {"account": self.account, "accountType": self.acct_type,
                "accountStatus": self.status, "availableCash": 40000.0,
                "buyingPower": 160000.0, "equity": self.equity}

    def _lots(self):
        if self.lots is not None:
            return self.lots
        return [{"positionId": f"p-{s}", "symbol": s, "securityType": "Stock",
                 "side": "Short" if q < 0 else "Long", "shares": abs(q), "priceAvg": 4.0}
                for s, q in self.pos.items() if q]

    def fill(self, cid):
        o = self.orders[cid]
        q = o["orderQuantity"]
        o.update(orderStatus="Filled", executed=q, leavesQuantity=0, priceAvg=4.01,
                 lastUpdated="2026-11-09T15:00:00.123Z")
        sym = o["symbol"]
        self.pos[sym] = self.pos.get(sym, 0) + (q if o["side"] == "Buy" else -q)
        if not self.pos[sym]:
            del self.pos[sym]

    def _place(self, body):
        cid = body["clientOrderId"]
        if cid in self.orders:
            return R(200, {"orderStatus": "Rejected", "text": "R114: Invalid duplicate UserOrderId",
                           "clientOrderId": cid, "canceledQuantity": 1, "executed": 0,
                           "leavesQuantity": 0})
        row = dict(body, accountId=self.account, orderStatus="PendingNew", executed=0,
                   leavesQuantity=body["orderQuantity"], priceAvg=0, text=None,
                   lastUpdated="2026-11-09T14:59:58.000Z", route=body.get("route") or "PAPER")
        if body["symbol"] in self.reject:
            row.update(orderStatus="Rejected", text=self.reject_text, leavesQuantity=0)
            self.orders[cid] = row
            return R(200, dict(row))
        self.orders[cid] = row
        self.gets_left[cid] = self.lag
        resp = dict(row)  # the POST answers with the initial state
        if self.hold:
            row["orderStatus"] = "New"  # acknowledged, resting
        else:
            self.fill(cid)
        return R(200, resp)

    # ---- the API
    def request(self, method, url, timeout=None, json=None, data=None):
        path = url.split("/v1/api", 1)[1]
        self.calls.append((method, path, json))
        if self.rate429 > 0:
            self.rate429 -= 1
            return R(429, text="", ctype="")
        if not self.auth_ok:
            return R(404, text="Not found", ctype="text/plain")
        a = f"/accounts/{self.account}"
        if method == "GET" and path == "/accounts":
            return R(200, {"accounts": [self._acct()]})
        if method == "GET" and path == f"{a}/pnl":
            return R(200, {"accountValue": self.equity + 1.5, "dayPnl": self.day_pnl,
                           "pnl": [{"positionId": lot["positionId"], "unrealizedPnL": -1.5}
                                   for lot in self._lots()]})
        if method == "GET" and path == f"{a}/positions":
            return R(200, {"positions": self._lots()})
        if method == "GET" and path == f"{a}/routes":
            return R(200, {"routes": [{"routeName": n, "orderTypes": ["Market", "Limit"]}
                                      for n in self.routes]})
        if method == "GET" and path == f"{a}/orders":
            return R(200, {"orders": list(self.orders.values())})
        if method == "POST" and path == f"{a}/order":
            if self.acct_type != "Paper" and not json.get("route"):
                return R(400, text="route is required on live accounts", ctype="text/plain")
            return self._place(json)
        if method == "GET" and path.startswith(f"{a}/order/"):
            cid = path.rsplit("/", 1)[1]
            o = self.orders.get(cid)
            if o is None or self.gets_left.get(cid, 0) > 0:
                self.gets_left[cid] = self.gets_left.get(cid, 0) - 1
                return R(404, text="Not found", ctype="text/plain")
            return R(200, dict(o))
        if method == "DELETE" and path.startswith(f"{a}/orders/"):
            cid = path.rsplit("/", 1)[1]
            o = self.orders.get(cid)
            if o is None or o["orderStatus"] in ("Filled", "Canceled", "Expired"):
                return R(404, text="Not found", ctype="text/plain")
            if o["orderStatus"] == "Rejected":  # the documented trap: 200 and the reason is gone
                o["text"] = "R130: Cancel Request Rejected: Original order not found or cancelable"
                return R(200, dict(o))
            o.update(orderStatus="Canceled", canceledQuantity=o["leavesQuantity"], leavesQuantity=0)
            return R(200, dict(o))
        if method == "GET" and path.startswith(f"{a}/is-easy-to-borrow/symbol/"):
            sym = path.rsplit("/", 1)[1]
            return R(200, {"isEasyToBorrow": self.etb.get(sym, True)})
        if method == "GET" and path == f"{a}/locates/inventory":
            if self.acct_type == "Paper":
                return R(200, [])
            return R(200, {"locateInventory": [{"symbol": s, "available": n, "toBeSold": 0,
                                                "unavailable": 0, "locateType": 1}
                                               for s, n in self.inventory.items() if n]})
        if method == "POST" and path == "/accounts/locates/quote":
            self.history.append({"accountId": json["account"], "symbol": json["symbol"],
                                 "quoteReqID": json["quoteReqID"], "locateShares": json["quantity"],
                                 "filledShares": 0, "locateStatus": 81, "locateType": 0,
                                 "locatePrice": 0, "locateError": 0, "text": None})
            self.polls[json["quoteReqID"]] = 0
            return R(200, {"locateQuoteSent": "true"})
        if method == "GET" and path == f"{a}/locates/history":
            for row in self.history:
                q = row["quoteReqID"]
                if row["locateStatus"] == 81:
                    self.polls[q] = self.polls.get(q, 0) + 1
                    if self.polls[q] >= self.offer_after:
                        if row["symbol"] in self.no_inventory:
                            row.update(locateStatus=56, locateError=11,
                                       text="11 No shares available.  Please try again later.")
                        else:
                            row.update(locateStatus=65, locateType=1, locatePrice=self.locate_price,
                                       text="")
            return R(200, {"locateHistory": [dict(r) for r in self.history]})
        if method == "POST" and path == "/accounts/locates/accept":
            for row in self.history:
                if row["quoteReqID"] == json["quoteReqID"] and row["locateStatus"] == 65:
                    row.update(locateStatus=50, filledShares=row["locateShares"])
                    self.inventory[row["symbol"]] = self.inventory.get(row["symbol"], 0) + row["locateShares"]
            return R(200, {"locateAcceptSent": "true"})
        return R(404, text="Not found", ctype="text/plain")


def tz_cfg(**kw):
    env = {"TZ_ACCOUNT": "TZP1", "TZ_API_KEY_ID": "pk-test", "TZ_API_SECRET_KEY": "sk-test"}
    env.update(kw)
    return tradezero.config_from_env(env)


def mk(fake=None, **kw):
    """A broker on a fake API; env-style keys (TZ_*) go to the config."""
    cfg_kw = {k: v for k, v in kw.items() if k.startswith("TZ_")}
    api_kw = {k: v for k, v in kw.items() if not k.startswith("TZ_")}
    fake = fake or FakeTZ(**api_kw)
    slept = []
    b = TradeZeroBroker(tz_cfg(**cfg_kw), session=fake, sleep=slept.append)
    b.fake, b.slept = fake, slept
    return b


def posted(fake):
    return [j for m, p, j in fake.calls if m == "POST" and p.endswith("/order")]


class ConfigTests(unittest.TestCase):
    def test_unset_means_not_linked(self):
        self.assertIsNone(tradezero.config_from_env({}))

    def test_account_without_keys_is_refused(self):
        with self.assertRaisesRegex(BrokerError, "TZ_API_KEY_ID"):
            tradezero.config_from_env({"TZ_ACCOUNT": "TZP1"})

    def test_defaults(self):
        c = tz_cfg()
        self.assertEqual((c.mode, c.book, c.books, c.max_shares, c.daily_loss_limit,
                          c.max_order_usd, c.order_type, c.route, c.locate_max_pct,
                          c.locate_max_usd_day), ("paper", "5m", ("5m",), 1, 100.0, 1000.0,
                                                  "market", None, 0.0, 5.0))

    def test_validation(self):
        with self.assertRaises(BrokerError):
            tz_cfg(TZ_MODE="real")
        with self.assertRaisesRegex(BrokerError, "TZ_BOOKS"):
            tz_cfg(TZ_BOOKS="1d")
        self.assertEqual(tz_cfg(TZ_BOOKS="5m, 15m,1h,5m").books, ("5m", "15m", "1h"))
        with self.assertRaises(BrokerError):
            tz_cfg(TZ_ORDER_TYPE="stop")
        with self.assertRaisesRegex(BrokerError, "above 0"):
            tz_cfg(TZ_MODE="live", TZ_DAILY_LOSS_LIMIT="0")
        with self.assertRaises(BrokerError):
            tz_cfg(TZ_MAX_SHARES="0")
        self.assertEqual(tz_cfg(TZ_ROUTE=" ARCA ").route, "ARCA")
        self.assertEqual(tz_cfg(TZ_LOCATE_MAX_PCT="0.5").locate_max_pct, 0.5)
        self.assertEqual((tz_cfg().shares, tz_cfg().max_shares), (None, 1))
        c = tz_cfg(TZ_SHARES="100")
        self.assertEqual((c.shares, c.max_shares), (100, 100))  # TZ_SHARES alone sets the cap too
        self.assertEqual(tz_cfg(TZ_SHARES="100", TZ_MAX_SHARES="300").max_shares, 300)
        with self.assertRaisesRegex(BrokerError, "above TZ_MAX_SHARES"):
            tz_cfg(TZ_SHARES="100", TZ_MAX_SHARES="1")
        with self.assertRaisesRegex(BrokerError, "replaced by TZ_LOCATE_MAX_PCT"):
            tz_cfg(TZ_LOCATE_MAX_PER_SHARE="0.05")  # the old per-share cap: refuse, don't ignore

    def test_only_the_tradezero_host(self):
        import dataclasses
        with self.assertRaisesRegex(BrokerError, "webapi.tradezero.com"):
            TradeZeroBroker(dataclasses.replace(tz_cfg(), base_url="https://evil.example/v1/api"))


class AccountTests(unittest.TestCase):
    def test_account_in_alpaca_shape_with_live_value(self):
        b = mk()
        a = b.account()
        self.assertEqual((a["equity"], a["cash"], a["buying_power"], a["status"],
                          a["account_id"], a["trading_blocked"], a["account_type"]),
                         (50001.5, 40000.0, 160000.0, "Active", "TZP1", False, "Paper"))
        self.assertEqual(b.day_pnl(), -3.25)
        self.assertEqual(b.fake.calls[0][:2], ("GET", "/accounts"))
        self.assertIn("TZ-API-KEY-ID", b.http.headers)

    def test_mode_must_match_what_tradezero_says(self):
        with self.assertRaisesRegex(BrokerError, "TZ_MODE=paper but TradeZero says TZP1 is a Margin"):
            mk(acct_type="Margin").account()
        with self.assertRaisesRegex(BrokerError, "TZ_MODE=live but TradeZero says TZP1 is a Paper"):
            mk(TZ_MODE="live").account()
        b = mk(TZ_MODE="live", acct_type="Margin")
        self.assertEqual(b.account()["account_type"], "Margin")
        self.assertEqual((b.live, b.venue), (True, "TradeZero LIVE"))

    def test_keys_that_dont_see_the_account(self):
        with self.assertRaisesRegex(BrokerError, "don't see account TZP1 .*TZP9"):
            mk(account="TZP9").account()
        with self.assertRaisesRegex(BrokerError, "TZ_API_KEY_ID"):
            mk(auth_ok=False).account()

    def test_positions_sum_lots_in_alpaca_shape(self):
        b = mk(pos={"AAA": -1, "BBB": 2})
        self.assertEqual(b.positions(), {
            "AAA": {"qty": "-1", "avg_entry_price": "4.0", "unrealized_pl": "-1.5"},
            "BBB": {"qty": "2", "avg_entry_price": "4.0", "unrealized_pl": "-1.5"}})
        lots = [{"positionId": "1", "symbol": "SOBR", "securityType": "Stock", "side": "Short",
                 "shares": 1, "priceAvg": 5.0},
                {"positionId": "2", "symbol": "SOBR", "securityType": "Stock", "side": "Short",
                 "shares": 1, "priceAvg": 7.0},
                {"positionId": "3", "symbol": "QQQ", "securityType": "Option", "side": "Long",
                 "shares": 1, "priceAvg": 1.0}]
        b = mk(lots=lots)
        self.assertEqual(b.positions(), {"SOBR": {"qty": "-2", "avg_entry_price": "6.0",
                                                  "unrealized_pl": "-3.0"}})

    def test_rate_limit_waits_then_gives_up(self):
        b = mk(rate429=1)
        b.account()
        self.assertEqual(b.slept[:1], [2.0])
        b = mk(rate429=3)
        with self.assertRaisesRegex(BrokerError, "429"):
            b.account()
        self.assertEqual(b.slept[:2], [2.0, 4.0])

    def test_calls_of_one_kind_are_spaced(self):
        b = mk()
        b.positions()
        b.positions()
        self.assertTrue(any(0 < s <= tradezero.SPACING["positions"] for s in b.slept))


class OrderTests(unittest.TestCase):
    def test_short_entry_is_sell_open_market_day(self):
        b = mk(lag=2)
        o = b.submit("aaa", 1, "sell", "td-AAA-abc123", ref_price=10.0)
        self.assertEqual((o["status"], o["filled_qty"], o["filled_avg_price"], o["id"],
                          o["client_order_id"]), ("filled", "1.0", "4.01", "td-AAA-abc123",
                                                  "td-AAA-abc123"))
        self.assertTrue(o["sent_at"] and o["filled_at"])
        body = posted(b.fake)[0]
        self.assertEqual(body, {"securityType": "Stock", "symbol": "AAA", "side": "Sell",
                                "openClose": "Open", "orderType": "Market", "orderQuantity": 1,
                                "timeInForce": "Day", "clientOrderId": "td-AAA-abc123"})
        self.assertNotIn("route", body)
        gets = [p for m, p, _j in b.fake.calls if m == "GET" and "/order/" in p]
        self.assertEqual(len(gets), 3)  # two 404s, then the registered order
        self.assertEqual(b.fake.pos, {"AAA": -1})

    def test_cover_is_buy_close_and_needs_no_price(self):
        b = mk(pos={"AAA": -1})
        o = b.submit("AAA", 1, "buy", "td-AAA-cov")
        self.assertEqual(o["status"], "filled")
        self.assertEqual(posted(b.fake)[0]["openClose"], "Close")
        self.assertEqual(posted(b.fake)[0]["side"], "Buy")
        self.assertNotIn("AAA", b.fake.pos)

    def test_short_only_cap_and_crossing_zero_are_refused_before_any_post(self):
        b = mk(pos={"AAA": -1, "LNG": 1})
        with self.assertRaisesRegex(BrokerError, "cap is 1"):
            b.submit("AAA", 1, "sell", "c1", ref_price=5)
        with self.assertRaisesRegex(BrokerError, "short only"):
            b.submit("NEW", 1, "buy", "c2", ref_price=5)
        with self.assertRaisesRegex(BrokerError, "cross through zero"):
            b.submit("LNG", 2, "sell", "c3", ref_price=5)
        self.assertEqual(posted(b.fake), [])
        b.submit("LNG", 1, "sell", "c4")  # closing the long is allowed, with no price
        self.assertEqual(posted(b.fake)[0]["openClose"], "Close")

    def test_cap_per_book_from_brokersync_bounded_by_the_listed_books(self):
        b = mk(TZ_BOOKS="5m,15m", pos={"AAA": -1})
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0, cap=2)  # two books: -2 is allowed
        self.assertEqual(b.fake.pos, {"AAA": -2})
        with self.assertRaisesRegex(BrokerError, "cap is 2 \\(1 per book"):
            b.submit("AAA", 1, "sell", "c2", ref_price=5.0, cap=9)  # never above cap x books
        b = mk(pos={"AAA": -1})
        with self.assertRaisesRegex(BrokerError, "cap is 1"):
            b.submit("AAA", 1, "sell", "c3", ref_price=5.0, cap=3)  # one listed book

    def test_order_value_cap_uses_the_reference_price(self):
        b = mk()
        with self.assertRaisesRegex(BrokerError, "TZ_MAX_ORDER_USD"):
            b.submit("BIG", 1, "sell", "c1", ref_price=1500.0)
        with self.assertRaisesRegex(BrokerError, "no reference price"):
            b.submit("BIG", 1, "sell", "c2")
        self.assertEqual(posted(b.fake), [])
        b.submit("BIG", 1, "sell", "c3", ref_price=999.0)
        self.assertEqual(len(posted(b.fake)), 1)

    def test_limit_mode_prices_off_the_model_and_falls_back_to_market_on_a_blind_cover(self):
        b = mk(TZ_ORDER_TYPE="limit", pos={"AAA": -1})
        b.submit("BBB", 1, "sell", "c1", ref_price=10.0)
        body = posted(b.fake)[0]
        self.assertEqual((body["orderType"], body["limitPrice"]), ("Limit", 9.7))
        self.assertEqual(b.reprice_s, 45)
        b.submit("AAA", 1, "buy", "c2")  # a cover with no price: market, never refused
        self.assertEqual(posted(b.fake)[1]["orderType"], "Market")
        b.submit("AAA", 1, "buy", "c3", order_type="market") if False else None
        self.assertEqual(mk().reprice_s, 0)  # market mode: nothing to re-price

    def test_stop_cover_order_type_override(self):
        b = mk(TZ_ORDER_TYPE="limit", pos={"AAA": -1})
        b.submit("AAA", 1, "buy", "c1", ref_price=10.0, order_type="market")
        self.assertEqual(posted(b.fake)[0]["orderType"], "Market")

    def test_rejection_raises_with_tradezeros_text_and_sent_at(self):
        b = mk(reject={"HTB"})
        with self.assertRaisesRegex(BrokerError, "R62: Short sale not permitted") as cm:
            b.submit("HTB", 1, "sell", "c1", ref_price=5.0)
        self.assertTrue(cm.exception.sent_at)
        self.assertEqual(b.fake.pos, {})

    def test_duplicate_client_order_id_is_a_rejection(self):
        b = mk()
        b.submit("AAA", 1, "sell", "same", ref_price=5.0)
        with self.assertRaisesRegex(BrokerError, "R114"):
            b.submit("BBB", 1, "sell", "same", ref_price=5.0)
        with self.assertRaisesRegex(BrokerError, "1-36 characters"):
            b.submit("BBB", 1, "sell", "x" * 37, ref_price=5.0)

    def test_inactive_account_sends_nothing(self):
        b = mk(status="Restricted")
        with self.assertRaisesRegex(BrokerError, "Restricted, not Active"):
            b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(posted(b.fake), [])
        self.assertTrue(b.account()["trading_blocked"])

    def test_live_orders_carry_a_route_the_account_offers(self):
        b = mk(TZ_MODE="live", acct_type="Margin")
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(posted(b.fake)[0]["route"], "SMART")
        b.submit("BBB", 1, "sell", "c2", ref_price=5.0)
        self.assertEqual(len([p for _m, p, _j in b.fake.calls if p.endswith("/routes")]), 1)
        b = mk(TZ_MODE="live", TZ_ROUTE="ARCA", acct_type="Margin")
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(posted(b.fake)[0]["route"], "ARCA")
        b = mk(TZ_MODE="live", acct_type="Margin", routes=("EDGX",))
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(posted(b.fake)[0]["route"], "EDGX")
        b = mk(TZ_MODE="live", TZ_ROUTE="NOPE", acct_type="Margin")
        with self.assertRaisesRegex(BrokerError, "set TZ_ROUTE"):
            b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(posted(b.fake), [])
        b = mk(TZ_MODE="live", acct_type="Margin", routes=("EDGX", "ARCA"))
        with self.assertRaisesRegex(BrokerError, "not SMART"):
            b.submit("AAA", 1, "sell", "c1", ref_price=5.0)

    def test_get_order_404_is_pending_right_after_a_send_and_expired_otherwise(self):
        b = mk(hold=True, lag=99)
        o = b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        self.assertEqual(o["status"], "accepted")  # never registered within the poll budget
        self.assertEqual(b.get_order("c1")["status"], "accepted")
        self.assertEqual(b.get_order("never-sent")["status"], "expired")
        self.assertIn("earlier session", b.get_order("never-sent")["message"])

    def test_open_orders_lists_working_orders_only(self):
        b = mk(hold=True)
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        b.fake.fill("c1")
        b.submit("BBB", 1, "sell", "c2", ref_price=5.0)
        self.assertEqual([o["id"] for o in b.open_orders()], ["c2"])
        self.assertEqual(b.get_order("c1")["status"], "filled")

    def test_cancel_never_touches_a_rejected_or_finished_order(self):
        b = mk(hold=True, reject={"HTB"})
        b.submit("AAA", 1, "sell", "c1", ref_price=5.0)
        b.cancel("c1")
        self.assertEqual(b.get_order("c1")["status"], "canceled")
        with self.assertRaises(BrokerError):
            b.submit("HTB", 1, "sell", "c2", ref_price=5.0)
        b.cancel("c2")
        b.cancel("unknown")
        deletes = [p for m, p, _j in b.fake.calls if m == "DELETE"]
        self.assertEqual(deletes, ["/accounts/TZP1/orders/c1"])
        self.assertIn("R62", b.get_order("c2")["message"])  # the reason survived


class BorrowTests(unittest.TestCase):
    def test_easy_to_borrow_goes_straight_through(self):
        b = mk()
        self.assertIsNone(b.short_check("AAA", 1))
        self.assertEqual([p for _m, p, _j in b.fake.calls if "borrow" in p],
                         ["/accounts/TZP1/is-easy-to-borrow/symbol/AAA"])

    def test_paper_skips_a_hard_to_borrow_name(self):
        b = mk(etb={"HTB": False})
        self.assertIn("paper accounts can't locate", b.short_check("HTB", 1))
        self.assertFalse(any("locates" in p for _m, p, _j in b.fake.calls))

    def test_live_without_a_locate_budget_skips(self):
        b = mk(TZ_MODE="live", acct_type="Margin", etb={"HTB": False})
        self.assertIn("TZ_LOCATE_MAX_PCT", b.short_check("HTB", 1))
        self.assertFalse(any("quote" in p for _m, p, _j in b.fake.calls))

    def test_no_model_price_means_no_locate(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="1", acct_type="Margin", etb={"HTB": False})
        self.assertIn("no price for it yet", b.short_check("HTB", 1))
        self.assertIn("no price for it yet", b.short_check("HTB", 1, ref_price=0))
        self.assertFalse(any("quote" in p for _m, p, _j in b.fake.calls))

    def test_existing_inventory_is_enough(self):
        b = mk(TZ_MODE="live", acct_type="Margin", etb={"HTB": False}, inventory={"HTB": 100})
        self.assertIsNone(b.short_check("HTB", 1))

    def test_live_buys_a_locate_within_the_caps(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="0.5", acct_type="Margin",
               etb={"HTB": False}, locate_price=0.01, offer_after=2)
        self.assertIsNone(b.short_check("HTB", 1, ref_price=5.0))  # 0.2% of $5
        quote = [j for m, p, j in b.fake.calls if p == "/accounts/locates/quote"][0]
        self.assertEqual((quote["account"], quote["symbol"], quote["quantity"]), ("TZP1", "HTB", 100))
        accept = [j for m, p, j in b.fake.calls if p == "/accounts/locates/accept"][0]
        self.assertEqual((accept["accountId"], accept["quoteReqID"]), ("TZP1", quote["quoteReqID"]))
        self.assertEqual(b.fake.inventory, {"HTB": 100})
        notes = b.drain_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("bought a locate for 100 HTB at $0.0100/share (0.20% of the $5.00 price; "
                      "$1.00)", notes[0][1])
        self.assertEqual(b.drain_notes(), [])
        self.assertEqual(b.locate_spend_today(), 1.0)

    def test_locate_fee_cap_is_a_percent_of_the_share_price(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="0.5", acct_type="Margin",
               etb={"HTB": False}, locate_price=0.05)
        why = b.short_check("HTB", 1, ref_price=5.0)  # 1% of a $5 stock: too dear
        self.assertIn("$0.0500/share = 1.00% of the $5.00 share price, above TZ_LOCATE_MAX_PCT 0.50% "
                      "($5.00 for 100)", why)
        self.assertFalse(any(p == "/accounts/locates/accept" for _m, p, _j in b.fake.calls))
        self.assertEqual(b.fake.inventory, {})
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="0.5", acct_type="Margin",
               etb={"EXP": False}, locate_price=0.05)
        self.assertIsNone(b.short_check("EXP", 1, ref_price=200.0))  # the same 5 cents on $200: 0.025%
        self.assertEqual(b.fake.inventory, {"EXP": 100})

    def test_daily_locate_budget(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="1", TZ_LOCATE_MAX_USD_DAY="1.5",
               acct_type="Margin", etb={"HTB": False, "HTC": False}, locate_price=0.01)
        self.assertIsNone(b.short_check("HTB", 1, ref_price=5.0))  # $1.00
        why = b.short_check("HTC", 1, ref_price=5.0)                # another $1.00 would pass $1.50
        self.assertIn("TZ_LOCATE_MAX_USD_DAY", why)
        self.assertEqual(b.fake.inventory, {"HTB": 100})

    def test_no_shares_offered(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="1", acct_type="Margin",
               etb={"HTB": False}, no_inventory={"HTB"})
        why = b.short_check("HTB", 1, ref_price=5.0)
        self.assertIn("offered no shares of HTB", why)
        self.assertIn("No shares available", why)

    def test_one_quote_per_symbol_per_30s(self):
        b = mk(TZ_MODE="live", TZ_LOCATE_MAX_PCT="1", acct_type="Margin",
               etb={"HTB": False}, no_inventory={"HTB"})
        b.short_check("HTB", 1, ref_price=5.0)
        self.assertIn("one per 30 s", b.short_check("HTB", 1, ref_price=5.0))
        self.assertEqual(len([p for _m, p, _j in b.fake.calls if p.endswith("locates/quote")]), 1)


class SyncTests(unittest.TestCase):
    """BrokerSync / LiveSync driving the TradeZero adapter."""
    NOW = datetime(2026, 11, 9, 11, 32, tzinfo=ET)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.stores = {k: Store(f"{self.dir.name}/{k}.db") for k in ("5m", "live")}
        self.eng = Engine(self.stores["5m"], ScriptedProvider([]), 5)

    def tearDown(self):
        for st in self.stores.values():
            st.db.close()
        self.dir.cleanup()

    def add_live(self, sym, last_price=10.0):
        self.eng.add_symbols(sym, "short_only", 1.0, self.NOW)
        sid = self.stores["5m"].q("SELECT id FROM symbols WHERE symbol=?", (sym,))[0]["id"]
        self.eng.set_live(sid, ["EMA5_10"], self.NOW)
        self.stores["5m"].x("UPDATE symbols SET last_price=? WHERE id=?", (last_price, sid))
        return sid

    def close_all(self):
        self.stores["5m"].x("UPDATE tranches SET status='closed', exit_time=?, exit_price=10.2, "
                            "exit_reason='cross: 5 EMA above 10 EMA'", (self.NOW.isoformat(),))

    def short(self, sid, sym, qty=100):
        self.stores["5m"].x(
            """INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time, entry_price,
               stop_price, risk_dollars, fee_through, trigger) VALUES (?,?,?,?,?,?,10,11,100,
               '2026-11-09','cross')""", (sid, sym, "EMA5_10", "short", qty, self.NOW.isoformat()))

    def link(self, **kw):
        self.b = mk(**kw)
        return LiveSync(self.stores["live"], self.b, {"5m": ("5-min", self.stores["5m"])},
                        live_book="5m", live_books=self.b.cfg.books, sleeves=LIVE_SLEEVES,
                        fill_wait_s=0, max_shares=self.b.max_shares,
                        daily_loss_limit=self.b.daily_loss_limit)

    def test_mirrors_the_live_short_at_one_share_with_the_models_price_as_reference(self):
        sid = self.add_live("AAA", last_price=12.5)
        self.short(sid, "AAA")
        live = self.link()
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {"AAA": -1})
        body = posted(self.b.fake)[0]
        self.assertEqual((body["side"], body["openClose"], body["orderType"]), ("Sell", "Open", "Market"))
        self.assertTrue(body["clientOrderId"].startswith("td-AAA-"))
        live.sync(self.NOW)
        self.assertEqual(len(posted(self.b.fake)), 1)
        st = live.status()
        self.assertEqual((st["venue"], st["live"], st["account"]["equity"]), ("TradeZero paper", False, 50001.5))
        self.assertTrue(all(r["match"] for r in st["reconciliation"]))
        row = self.stores["live"].q("SELECT status, filled_qty, book FROM orders")[0]
        self.assertEqual((row["status"], row["filled_qty"], row["book"]), ("filled", 1.0, "5m"))
        self.assertEqual(live._ref_price("AAA"), 12.5)
        self.close_all()
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {})
        self.assertEqual(posted(self.b.fake)[1]["openClose"], "Close")

    def test_fixed_size_trades_the_locate_lot_whatever_the_model_holds(self):
        """TZ_SHARES=100: the model short 37 or 150, TradeZero short exactly 100,
        the locate is one 100-share lot, and the cover is the whole 100."""
        sid = self.add_live("HTB", last_price=8.0)
        self.short(sid, "HTB", qty=37)
        live = self.link(TZ_SHARES="100", TZ_MAX_ORDER_USD="2500", TZ_LOCATE_MAX_PCT="1",
                         TZ_MODE="live", acct_type="Margin", etb={"HTB": False}, locate_price=0.02)
        self.assertEqual(live.desired(), {"HTB": -100})
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {"HTB": -100})
        body = posted(self.b.fake)[0]
        self.assertEqual((body["orderQuantity"], body["side"], body["openClose"]), (100, "Sell", "Open"))
        quote = [j for m, p, j in self.b.fake.calls if p == "/accounts/locates/quote"][0]
        self.assertEqual(quote["quantity"], 100)
        self.assertEqual(live.status()["control"]["max_shares"], 100)
        self.assertTrue(all(r["match"] for r in live.status()["reconciliation"]))
        self.stores["5m"].x("UPDATE tranches SET qty=150")  # the model's size never matters
        live.sync(self.NOW)
        self.assertEqual(len(posted(self.b.fake)), 1)
        self.close_all()
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {})
        self.assertEqual((posted(self.b.fake)[1]["orderQuantity"], posted(self.b.fake)[1]["openClose"]),
                         (100, "Close"))

    def test_fixed_size_still_obeys_the_order_value_cap(self):
        sid = self.add_live("BIG", last_price=40.0)  # 100 x $40 = $4,000 > $2,500
        self.short(sid, "BIG")
        live = self.link(TZ_SHARES="100", TZ_MAX_ORDER_USD="2500")
        live.sync(self.NOW)
        self.assertEqual(posted(self.b.fake), [])
        self.assertIn("TZ_MAX_ORDER_USD", self.stores["live"].q("SELECT message FROM orders")[0]["message"])

    def test_an_entry_without_a_model_price_is_refused_and_logged_not_sent(self):
        sid = self.add_live("AAA", last_price=None)
        self.short(sid, "AAA")
        live = self.link()
        live.sync(self.NOW)
        self.assertEqual(posted(self.b.fake), [])
        rej = self.stores["live"].q("SELECT status, message FROM orders")[0]
        self.assertEqual(rej["status"], "rejected")
        self.assertIn("no reference price", rej["message"])

    def test_not_easy_to_borrow_skips_the_entry_but_never_a_cover(self):
        sid = self.add_live("HTB")
        self.short(sid, "HTB")
        live = self.link(etb={"HTB": False})
        live.sync(self.NOW)
        self.assertEqual(posted(self.b.fake), [])
        self.assertIn("not easy to borrow", live.status()["reconciliation"][0]["note"])
        self.close_all()
        self.b.fake.pos["HTB"] = -1  # a short opened earlier is still covered...
        live.accept_position("HTB", self.NOW)  # ...once the link owns it (position guard)
        live.sync(self.NOW)
        self.assertEqual(posted(self.b.fake)[0]["openClose"], "Close")

    def test_a_bought_locate_is_in_the_dashboard_log(self):
        sid = self.add_live("HTB")
        self.short(sid, "HTB")
        live = self.link(TZ_MODE="live", TZ_LOCATE_MAX_PCT="0.5", acct_type="Margin",
                         etb={"HTB": False}, locate_price=0.01)
        live.sync(self.NOW)  # the book's last price ($10) is the reference: 0.1%
        self.assertEqual(self.b.fake.pos, {"HTB": -1})
        self.assertEqual(posted(self.b.fake)[0]["route"], "SMART")
        msgs = [e["message"] for e in self.stores["live"].q("SELECT message FROM events WHERE kind='broker'")]
        self.assertTrue(any("bought a locate for 100 HTB at $0.0100/share (0.10% of the $10.00 price"
                            in m for m in msgs), msgs)
        self.assertEqual(live.status()["venue"], "TradeZero LIVE")

    def test_another_venues_day_start_and_halt_do_not_carry_over(self):
        """The live store is shared across venues. Today's start equity latched
        by the IBKR link ($4,971) must not make TradeZero's $2,500 look like a
        $2,471 loss (2026-11-10), and IBKR's halt is IBKR's; an untagged halt
        from an older version still halts."""
        day = self.NOW.date().isoformat()
        sid = self.add_live("AAA")
        self.short(sid, "AAA")
        ib_tag = "IB Gateway 127.0.0.1:4001, account U1 (live)"
        self.stores["live"].x("INSERT OR REPLACE INTO broker_control VALUES ('day_start', ?)",
                              (f"{day}|4971.0|{ib_tag}",))
        self.stores["live"].x("INSERT OR REPLACE INTO broker_control VALUES ('halt', ?)",
                              (f"{day}|{ib_tag}|daily loss limit hit: IBKR LIVE day P&L -101.00 vs -100",))
        live = self.link()
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {"AAA": -1})  # traded: neither latch was ours
        self.assertEqual(live.day_start(self.NOW), 50001.5)
        self.assertIsNone(live.stop_reason(self.NOW))
        self.assertEqual(self.stores["live"].q("SELECT value FROM broker_control WHERE key='day_start'")
                         [0]["value"], f"{day}|50001.5|{self.b.endpoint}")
        # (status() reads the latch against the real clock's date, so it is not checked here)
        # an untagged day_start (older version) is stale too: re-latched, no halt
        self.stores["live"].x("UPDATE broker_control SET value=? WHERE key='day_start'", (f"{day}|4971.0",))
        live.sync(self.NOW)
        self.assertEqual(live.day_start(self.NOW), 50001.5)
        self.assertIsNone(live.stop_reason(self.NOW))
        # an untagged halt still halts, and our own tagged halt halts
        self.stores["live"].x("UPDATE broker_control SET value=? WHERE key='halt'", (f"{day}|old-style halt",))
        self.assertEqual(live.stop_reason(self.NOW), ("flatten", "old-style halt; orders resume tomorrow"))
        self.stores["live"].x("UPDATE broker_control SET value=? WHERE key='halt'",
                              (f"{day}|{self.b.endpoint}|daily loss limit hit: ours",))
        self.assertEqual(live.stop_reason(self.NOW)[1], "daily loss limit hit: ours; orders resume tomorrow")
        self.stores["live"].x("UPDATE broker_control SET value=? WHERE key='halt'", ("2026-11-08|x|yesterday",))
        self.assertIsNone(live.stop_reason(self.NOW))

    def test_the_loss_limit_still_trips_and_latches_with_the_tag(self):
        sid = self.add_live("AAA")
        self.short(sid, "AAA")
        live = self.link()
        live.sync(self.NOW)
        self.b.fake.equity = 50001.5 - 1.5 - 150  # accountValue drops $150
        self.b._pnl = None
        live.sync(self.NOW)
        self.assertEqual(self.b.fake.pos, {})  # flattened
        why = live.stop_reason(self.NOW)
        self.assertEqual(why[0], "flatten")
        self.assertIn("daily loss limit hit: TradeZero paper day P&L -150.00 vs -100", why[1])
        self.assertTrue(self.stores["live"].q("SELECT value FROM broker_control WHERE key='halt'")
                        [0]["value"].startswith(f"{self.NOW.date().isoformat()}|{self.b.endpoint}|"))

    def test_a_tradezero_rejection_holds_back_new_shorts_for_the_day(self):
        sid = self.add_live("HTB")
        self.short(sid, "HTB")
        live = self.link(reject={"HTB"}, reject_text="R62: not easy to borrow; no shares available")
        live.sync(self.NOW)
        live.sync(self.NOW)
        self.assertEqual(len(posted(self.b.fake)), 1)
        self.assertIn("can't short it today", live.blocked(self.NOW)["HTB"])


class AppTests(unittest.TestCase):
    KEYS = ("IBKR_ACCOUNT", "IBKR_MODE", "TZ_ACCOUNT", "TZ_MODE", "TZ_API_KEY_ID",
            "TZ_API_SECRET_KEY", "LIVE_BROKER")

    def setUp(self):
        self.old = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_link_live_picks_the_configured_venue(self):
        self.assertEqual(app.link_live(False), (None, app.LIVE_NOTE))
        os.environ.update({"TZ_ACCOUNT": "TZP1", "TZ_API_KEY_ID": "k", "TZ_API_SECRET_KEY": "s"})
        b, note = app.link_live(False)
        self.assertIsInstance(b, TradeZeroBroker)
        self.assertEqual((note, b.venue), (None, "TradeZero paper"))
        self.assertIsNone(app.link_live(True)[0])
        os.environ["IBKR_ACCOUNT"] = "DU123"
        b, note = app.link_live(False)
        self.assertIsNone(b)
        self.assertIn("LIVE_BROKER", note)
        os.environ["LIVE_BROKER"] = "tradezero"
        self.assertIsInstance(app.link_live(False)[0], TradeZeroBroker)
        os.environ["LIVE_BROKER"] = "ibkr"
        self.assertEqual(app.live_broker_choice(), "ibkr")
        os.environ["LIVE_BROKER"] = "etrade"
        self.assertIn("must be ibkr or tradezero", app.link_live(False)[1])
        os.environ["LIVE_BROKER"] = "tradezero"
        os.environ.pop("TZ_ACCOUNT")
        self.assertIn("TZ_ACCOUNT is not set", app.link_live(False)[1])

    def test_bad_tradezero_settings_leave_it_unlinked(self):
        os.environ.update({"TZ_ACCOUNT": "TZP1"})
        b, note = app.link_live(False)
        self.assertIsNone(b)
        self.assertIn("TZ_API_KEY_ID", note)

    def test_live_state_names_the_venue(self):
        d = tempfile.TemporaryDirectory()
        try:
            stores = {"5m": Store(f"{d.name}/5m.db"), "live": Store(f"{d.name}/live.db")}
            b = mk()
            live = LiveSync(stores["live"], b, {"5m": ("5-min", stores["5m"])}, live_book="5m",
                            live_books=("5m",), sleeves=LIVE_SLEEVES, fill_wait_s=0,
                            max_shares=1, daily_loss_limit=100)
            st = app.live_state(live)
            self.assertEqual((st["name"], st["venue"], st["configured"]),
                             ("TradeZero", "TradeZero paper", True))
            self.assertIn("webapi.tradezero.com", st["endpoint"])
            self.assertIn("TradeZero paper: OK", app.live_check(live, None))
            for st_ in stores.values():
                st_.db.close()
        finally:
            d.cleanup()

    def test_secrets_are_redacted(self):
        from data import redact
        os.environ["TZ_API_SECRET_KEY"] = "sk-very-secret"
        self.assertEqual(redact("key sk-very-secret leaked"), "key *** leaked")


if __name__ == "__main__":
    unittest.main()
