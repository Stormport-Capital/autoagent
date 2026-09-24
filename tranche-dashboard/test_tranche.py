"""Offline tests: indicator math, the Russo VWAP-fail trigger, and the engine's
entries, sizing, stops, borrow fees and gating. Run: python test_tranche.py"""

import math
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

from broker import AlpacaPaper, BrokerError, BrokerSync
from engine import Engine, ValidationError, vwap_fail
from indicators import (ET, Bar, atr, crossed_below, ema, resample_hourly,
                        session_hour_ends, session_vwap)
from store import Store

FIVE = timedelta(minutes=5)


def hour_bars(day: date, hour_idx: int, o, h, l, c, vol=1000.0) -> list[Bar]:
    """Twelve (or six for the last hour) 5-minute bars forming one hourly bar."""
    start = datetime.combine(day, time(9, 30), tzinfo=ET) + timedelta(hours=hour_idx)
    n = 6 if hour_idx == 6 else 12
    out = []
    for k in range(n):
        t = start + k * FIVE
        po = o if k == 0 else c
        bh = h if k == 1 else max(po, c)
        bl = l if k == 2 else min(po, c)
        out.append(Bar(t, t + FIVE, po, bh, bl, c, vol))
    return out


def day_from_closes(day: date, closes: list[float], spread=0.2) -> list[Bar]:
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        bars += hour_bars(day, i, prev, max(prev, c) + spread, min(prev, c) - spread, c)
        prev = c
    return bars


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class ScriptedProvider:
    name = "scripted"

    def __init__(self, bars):
        self.bars = sorted(bars, key=lambda b: b.start)

    def five_min_bars(self, symbol, now):
        return [b for b in self.bars if b.end <= now]


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_recursion(self):
        e = ema([1, 2, 3, 4, 5, 6], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 0.5 * 4 + 0.5 * 2.0)

    def test_resample_rth_hours(self):
        d = date(2026, 9, 21)
        bars = day_from_closes(d, [10, 11, 12, 13, 14, 15, 16])
        pre = Bar(datetime(2026, 9, 21, 8, 0, tzinfo=ET), datetime(2026, 9, 21, 8, 5, tzinfo=ET),
                  1, 99, 1, 1, 5)
        hourly = resample_hourly(bars + [pre])
        self.assertEqual([b.end for b in hourly], session_hour_ends(d))
        self.assertEqual(hourly[0].open, 10)
        self.assertEqual(hourly[-1].close, 16)
        self.assertLess(max(b.high for b in hourly), 99)  # premarket excluded

    def test_session_vwap(self):
        d = date(2026, 9, 21)
        bars = hour_bars(d, 0, 10, 10, 10, 10, vol=100) + hour_bars(d, 1, 20, 20, 20, 20, vol=300)
        end1 = session_hour_ends(d)[1]
        self.assertAlmostEqual(session_vwap(bars, d, end1), (10 * 1200 + 20 * 3600) / 4800)

    def test_atr_and_cross(self):
        d = date(2026, 9, 21)
        hourly = resample_hourly(day_from_closes(d, [10] * 7) + day_from_closes(d + timedelta(1), [10] * 7))
        a = atr(hourly, 3)
        self.assertIsNotNone(a[3])
        self.assertTrue(crossed_below(2, 1, 0.5, 1))
        self.assertFalse(crossed_below(0.5, 1, 0.4, 1))


class VwapFailTests(unittest.TestCase):
    d = date(2026, 9, 21)

    def build(self, second):
        five = hour_bars(self.d, 0, 10, 12, 9.8, 11.5, vol=1000) + hour_bars(self.d, 1, *second, vol=4000)
        return resample_hourly(five), five

    def test_fires_on_lower_high_close_below_vwap(self):
        hourly, five = self.build((11.5, 11.8, 9.0, 9.2))
        self.assertLess(hourly[1].close, session_vwap(five, self.d, hourly[1].end))
        self.assertTrue(vwap_fail(hourly, five, 1))

    def test_green_bar_qualifies_no_red_candle_test(self):
        hourly, five = self.build((9.0, 11.0, 8.9, 9.3))
        self.assertGreater(hourly[1].close, hourly[1].open)
        self.assertTrue(vwap_fail(hourly, five, 1))

    def test_new_high_of_day_blocks(self):
        hourly, five = self.build((11.5, 12.5, 9.0, 9.2))
        self.assertFalse(vwap_fail(hourly, five, 1))

    def test_first_bar_of_session_never_fires(self):
        hourly, five = self.build((11.5, 11.8, 9.0, 9.2))
        self.assertFalse(vwap_fail(hourly, five, 0))

    def test_requires_earlier_close_above_vwap(self):
        five = hour_bars(self.d, 0, 10, 10.2, 8.0, 8.5) + hour_bars(self.d, 1, 8.5, 9.0, 8.0, 8.2)
        hourly = resample_hourly(five)
        self.assertFalse(vwap_fail(hourly, five, 1))


class EngineTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)
        self.store.save_settings({"slippage_bps": 0.0, "bar_close_delay_min": 0})

    def tearDown(self):
        self.store.db.close()
        os.remove(self.path)

    def engine(self, bars):
        return Engine(self.store, ScriptedProvider(bars))

    def uptrend_then_drop(self, days, drop_day_closes):
        """Steady uptrend for `days`, then one session of `drop_day_closes`."""
        ds = weekdays(date(2026, 8, 3), days + 1)
        bars, p = [], 50.0
        for d in ds[:-1]:
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        bars += day_from_closes(ds[-1], drop_day_closes(p))
        return bars, ds

    def test_rejects_bad_setup(self):
        eng = self.engine([])
        now = datetime(2026, 9, 21, 8, tzinfo=ET)
        with self.assertRaises(ValidationError):
            eng.add_symbols("AAPL", "short_only", 3.5, now)
        with self.assertRaises(ValidationError):
            eng.add_symbols("AAPL", "sideways", 1, now)
        with self.assertRaises(ValidationError):
            eng.add_symbols("$$$", "short_only", 1, now)

    def test_history_before_add_is_not_traded(self):
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1, p - 2, p - 3, p - 4, p - 5, p - 6, p - 7])
        eng = self.engine(bars)
        eng.add_symbols("XYZ", "short_only", 1.0, datetime.combine(ds[-1], time(17), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(17), tzinfo=ET))
        self.assertEqual(self.store.q("SELECT * FROM tranches"), [])

    def test_ema_cross_short_sizing_and_stop(self):
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 3, p + 6, p + 7, p + 8])
        eng = self.engine(bars)
        added = datetime.combine(ds[-1], time(9), tzinfo=ET)
        eng.add_symbols("XYZ", "short_only", 1.5, added)
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))

        first = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10' ORDER BY id")[0]
        self.assertEqual(first["side"], "short")
        # independently recompute size at the entry bar
        hourly = [b for b in resample_hourly(bars) if b.end <= datetime.fromisoformat(first["entry_time"])]
        a = atr(hourly, 14)[-1]
        dist = 1.5 * a
        self.assertAlmostEqual(first["stop_price"], first["entry_price"] + dist, places=6)
        self.assertEqual(first["qty"], math.floor(100_000 * 0.015 / 3 / dist))
        # the rip to p+6 blows through the stop: stopped at the gapped open or the stop
        self.assertEqual(first["exit_reason"], "stop hit")
        self.assertGreaterEqual(first["exit_price"], first["stop_price"])
        self.assertLess(first["gross_pnl"], 0)
        # short_only: the bullish recross never opens a long
        self.assertFalse(self.store.q("SELECT 1 FROM tranches WHERE side='long'"))

    def test_long_short_mode_opens_long_on_up_cross(self):
        ds = weekdays(date(2026, 8, 3), 10)
        bars, p = [], 80.0
        for d in ds[:-2]:  # downtrend
            closes = [p - 0.1 * (k + 1) for k in range(7)]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        bars += day_from_closes(ds[-2], [p + 1, p + 2, p + 3, p + 4, p + 5, p + 6, p + 7])
        bars += day_from_closes(ds[-1], [p + 8] * 7)
        eng = self.engine(bars)
        eng.add_symbols("UPP", "long_short", 1.0, datetime.combine(ds[-2], time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))
        longs = self.store.q("SELECT * FROM tranches WHERE side='long'")
        self.assertEqual({t["sleeve"] for t in longs}, {"EMA5_10", "EMA10_20"})
        self.assertFalse(self.store.q("SELECT 1 FROM tranches WHERE sleeve='VWAP' AND side='long'"))

    def test_borrow_fee_over_weekend(self):
        # short opened Friday, still open Monday -> 3 calendar nights charged
        fri = date(2026, 9, 18)
        bars, p = [], 50.0
        for d in weekdays(date(2026, 9, 1), 14):
            if d > fri:
                break
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            if d == fri:
                closes = [p - 1, p - 2, p - 3, p - 3.1, p - 3.2, p - 3.3, p - 3.4]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        mon = date(2026, 9, 21)
        bars += day_from_closes(mon, [p - 0.1] * 7)
        eng = self.engine(bars)
        eng.add_symbols("FEE", "short_only", 1.0, datetime.combine(fri, time(9), tzinfo=ET))
        eng.tick(datetime.combine(fri, time(16, 5), tzinfo=ET))
        t = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")[0]
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["borrow_fees"], 0)
        eng.tick(datetime.combine(mon, time(10, 35), tzinfo=ET))
        t = self.store.q("SELECT * FROM tranches WHERE id=?", (t["id"],))[0]
        expected = 3 * t["qty"] * p * 0.10 / 360  # marked at Friday's close
        self.assertAlmostEqual(t["borrow_fees"], expected, places=6)

    def test_leverage_cap_limits_size(self):
        self.store.save_settings({"max_leverage": 0.1})
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 4, p - 4, p - 4, p - 4])
        eng = self.engine(bars)
        eng.add_symbols("CAP", "short_only", 3.0, datetime.combine(ds[-1], time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))
        gross = sum(t["qty"] * t["entry_price"] for t in self.store.q("SELECT * FROM tranches"))
        self.assertLessEqual(gross, 100_000 * 0.1 + 1e-6)
        self.assertTrue(self.store.q("SELECT 1 FROM tranches"))

    def test_paused_symbol_skips_entries(self):
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 4, p - 4, p - 4, p - 4])
        eng = self.engine(bars)
        eng.add_symbols("PAU", "short_only", 1.0, datetime.combine(ds[-1], time(9), tzinfo=ET))
        eng.set_status(1, "paused", datetime.combine(ds[-1], time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))
        self.assertFalse(self.store.q("SELECT 1 FROM tranches"))
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE kind='skip'"))

    def test_tick_is_idempotent(self):
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 4, p - 4, p - 4, p - 4])
        eng = self.engine(bars)
        eng.add_symbols("IDM", "short_only", 1.0, datetime.combine(ds[-1], time(9), tzinfo=ET))
        now = datetime.combine(ds[-1], time(16, 5), tzinfo=ET)
        eng.tick(now)
        n = len(self.store.q("SELECT * FROM tranches"))
        eng.tick(now)
        self.assertEqual(len(self.store.q("SELECT * FROM tranches")), n)

    def russo_vwap(self, entry_day_rest, next_day=None):
        """10 flat sessions at 45 (daily 10-MA ~45), then a session whose second
        hour is a VWAP fail (short 49.5, HOD 52), then `entry_day_rest` for
        hours 2-6 and optionally a next session. Returns the VWAP tranches."""
        ds = weekdays(date(2026, 8, 3), 12)
        bars = []
        for d in ds[:10]:
            bars += day_from_closes(d, [45.0] * 7)
        d = ds[10]
        bars += hour_bars(d, 0, 48.0, 52.0, 47.8, 51.5)   # closes above VWAP, HOD 52
        bars += hour_bars(d, 1, 51.5, 51.8, 49.4, 49.5)   # VWAP fail -> short 49.5
        for k, spec in enumerate(entry_day_rest, start=2):
            bars += hour_bars(d, k, *spec)
        end = datetime.combine(d, time(16, 5), tzinfo=ET)
        if next_day:
            for k, spec in enumerate(next_day):
                bars += hour_bars(ds[11], k, *spec)
            end = datetime.combine(ds[11], time(16, 5), tzinfo=ET)
        eng = self.engine(bars)
        eng.add_symbols("VWP", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(end)
        return self.store.q("SELECT * FROM tranches WHERE sleeve='VWAP' ORDER BY id"), d, ds

    CALM = (49.3, 49.5, 48.8, 49.0)

    def test_vwap_entry_is_edge_triggered_with_hod_stop(self):
        v, d, _ = self.russo_vwap([(49.5, 49.9, 49.1, 49.3)] + [self.CALM] * 4)
        self.assertEqual(len(v), 1)  # still true on hour 3: no second entry
        t = v[0]
        self.assertEqual(t["side"], "short")
        self.assertEqual(t["entry_time"], session_hour_ends(d)[1].isoformat())
        self.assertAlmostEqual(t["entry_price"], 49.5)
        self.assertAlmostEqual(t["stop_price"], 52.0)          # session HOD
        self.assertAlmostEqual(t["target_price"], 45.0)        # daily 10-MA
        self.assertAlmostEqual(t["risk_dollars"], t["qty"] * 2.5)
        self.assertEqual(t["status"], "open")                  # held overnight

    def test_vwap_stop_on_new_high_of_day_fills_at_that_high(self):
        v, d, _ = self.russo_vwap([self.CALM, (49.0, 52.6, 48.9, 50.0)] + [self.CALM] * 3)
        t = v[0]
        self.assertTrue(t["exit_reason"].startswith("stop: new high of day"))
        self.assertAlmostEqual(t["exit_price"], 52.6)
        self.assertEqual(t["exit_time"], session_hour_ends(d)[3].isoformat())

    def test_vwap_stop_checked_before_target(self):
        v, _, _ = self.russo_vwap([(49.5, 52.5, 44.0, 45.0)] + [self.CALM] * 4)
        self.assertTrue(v[0]["exit_reason"].startswith("stop"))

    def test_vwap_target_next_session_after_overnight_hold(self):
        nxt = [(48.0, 48.5, 47.0, 47.5), (47.5, 47.6, 44.8, 45.5)] + [(45.5, 45.8, 45.2, 45.5)] * 5
        v, _, ds = self.russo_vwap([self.CALM] * 5, next_day=nxt)
        t = v[0]
        target = (9 * 45.0 + 49.0) / 10   # 10-MA through the entry day's close
        self.assertTrue(t["exit_reason"].startswith("target: daily 10-MA"))
        self.assertAlmostEqual(t["exit_price"], target)
        self.assertEqual(t["exit_time"], session_hour_ends(ds[11])[1].isoformat())
        self.assertGreater(t["borrow_fees"], 0)            # one night held

    def test_vwap_stop_next_session_needs_price_above_entry(self):
        # next day: first bar is its own HOD but below entry -> no stop;
        # second bar makes a new HOD above entry -> stopped at that high
        nxt = [(48.0, 48.5, 47.0, 47.5), (47.5, 50.2, 47.4, 50.0)] + [self.CALM] * 5
        v, _, ds = self.russo_vwap([self.CALM] * 5, next_day=nxt)
        t = v[0]
        self.assertTrue(t["exit_reason"].startswith("stop"))
        self.assertAlmostEqual(t["exit_price"], 50.2)
        self.assertEqual(t["exit_time"], session_hour_ends(ds[11])[1].isoformat())

    def test_vwap_below_target_still_enters_and_covers_next_open(self):
        ds = weekdays(date(2026, 8, 3), 11)
        bars = []
        for d in ds[:10]:
            bars += day_from_closes(d, [60.0] * 7)          # 10-MA 60, above price
        d = ds[10]
        bars += hour_bars(d, 0, 48.0, 52.0, 47.8, 51.5)
        bars += hour_bars(d, 1, 51.5, 51.8, 49.4, 49.5)     # VWAP fail below the 10-MA
        bars += hour_bars(d, 2, 49.6, 49.9, 49.2, 49.4)
        eng = self.engine(bars)
        eng.add_symbols("LOW", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(datetime.combine(d, time(12, 35), tzinfo=ET))
        v = self.store.q("SELECT * FROM tranches WHERE sleeve='VWAP'")
        self.assertEqual(len(v), 1)                        # no entry filter
        self.assertTrue(v[0]["exit_reason"].startswith("target"))
        self.assertAlmostEqual(v[0]["exit_price"], 49.6)   # next bar's open, not 60

    def test_vwap_target_gap_down_covers_at_open(self):
        nxt = [(44.0, 44.5, 43.5, 44.2)] + [(44.2, 44.4, 44.0, 44.2)] * 6
        v, _, ds = self.russo_vwap([self.CALM] * 5, next_day=nxt)
        self.assertTrue(v[0]["exit_reason"].startswith("target"))
        self.assertAlmostEqual(v[0]["exit_price"], 44.0)

    def test_behind_reports_unprocessed_hour(self):
        d = weekdays(date(2026, 8, 3), 1)[0]
        bars = day_from_closes(d, [50.0] * 7)
        cut = datetime.combine(d, time(10, 15), tzinfo=ET)   # delayed feed
        feed = ScriptedProvider([b for b in bars if b.end <= cut])
        eng = Engine(self.store, feed)
        eng.add_symbols("DLY", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        hour_end = session_hour_ends(d)[0]
        eng.tick(datetime.combine(d, time(10, 32), tzinfo=ET))
        self.assertTrue(eng.behind(hour_end))                 # still waiting
        feed.bars = bars                                        # data arrives
        eng.tick(datetime.combine(d, time(10, 47), tzinfo=ET))
        self.assertFalse(eng.behind(hour_end))

    def test_delayed_feed_does_not_close_partial_hour(self):
        d = weekdays(date(2026, 8, 3), 1)[0]
        bars = day_from_closes(d, [50.0] * 7)
        cut = datetime.combine(d, time(10, 15), tzinfo=ET)   # feed lags: data to 10:15 only
        eng = self.engine([b for b in bars if b.end <= cut])
        eng.add_symbols("DLY", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        with self.assertRaises(RuntimeError):
            eng._process_symbol(self.store.q("SELECT * FROM symbols")[0],
                                datetime.combine(d, time(10, 32), tzinfo=ET), self.store.settings())

    def test_equity_accounting(self):
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 3, p + 6, p + 7, p + 8])
        eng = self.engine(bars)
        eng.add_symbols("ACC", "short_only", 1.0, datetime.combine(ds[-1], time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))
        s = eng.summary()
        self.assertAlmostEqual(s["equity"], 100_000 + s["realized_net"] + s["unrealized"], places=6)


class FakeBroker:
    """In-memory stand-in for AlpacaPaper: market orders fill instantly
    unless `hold` is set, and `reject` names symbols to refuse."""

    def __init__(self, positions=None, hold=False, reject=()):
        self.pos = dict(positions or {})
        self.hold, self.reject = hold, set(reject)
        self.orders, self.sent = {}, []

    def positions(self):
        return {s: {"qty": str(q), "avg_entry_price": "10", "unrealized_pl": "0"}
                for s, q in self.pos.items() if q}

    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] == "accepted"]

    def account(self):
        return {"equity": "100000", "cash": "100000", "buying_power": "200000",
                "last_equity": "100000", "status": "ACTIVE", "shorting_enabled": True}

    def submit(self, symbol, qty, side, cid):
        if symbol in self.reject:
            raise BrokerError("Alpaca 403: asset not shortable")
        self.sent.append((symbol, side, qty))
        oid = f"o{len(self.orders)}"
        o = {"id": oid, "symbol": symbol, "status": "accepted", "filled_qty": "0",
             "filled_avg_price": None, "filled_at": None}
        self.orders[oid] = o
        if not self.hold:
            self.fill(oid)
        return dict(o)

    def fill(self, oid):
        o = self.orders[oid]
        o.update(status="filled", filled_qty="1", filled_avg_price="10")
        sym, side, qty = self.sent[int(oid[1:])]
        self.pos[sym] = self.pos.get(sym, 0) + (qty if side == "buy" else -qty)

    def get_order(self, oid):
        return dict(self.orders[oid])


class BrokerSyncTests(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 11, 32, tzinfo=ET)

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)
        self.eng = Engine(self.store, ScriptedProvider([]))

    def tearDown(self):
        self.store.db.close()
        os.remove(self.path)

    def add(self, sym):
        self.eng.add_symbols(sym, "long_short", 1.0, self.NOW)
        return self.store.q("SELECT id FROM symbols WHERE symbol=?", (sym,))[0]["id"]

    def tranche(self, sym, sleeve, side, qty):
        sid = self.store.q("SELECT id FROM symbols WHERE symbol=?", (sym,))[0]["id"]
        self.store.x("""INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
                        entry_price, stop_price, risk_dollars, fee_through)
                        VALUES (?,?,?,?,?,?,10,11,100,'2026-09-21')""",
                     (sid, sym, sleeve, side, qty, self.NOW.isoformat()))

    def test_refuses_live_endpoint(self):
        with self.assertRaises(BrokerError):
            AlpacaPaper("k", "s", base_url="https://api.alpaca.markets")
        AlpacaPaper("k", "s")  # the paper default is accepted

    def test_mirrors_net_position_and_is_idempotent(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        self.tranche("AAA", "VWAP", "short", 50)
        fb = FakeBroker()
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        sync.sync(self.NOW)
        self.assertEqual(fb.sent, [("AAA", "sell", 150)])
        sync.sync(self.NOW)
        self.assertEqual(len(fb.sent), 1)
        self.assertEqual(self.store.q("SELECT status FROM orders")[0]["status"], "filled")

    def test_leaves_unmanaged_symbols_alone(self):
        fb = FakeBroker(positions={"MANUAL": 300})
        BrokerSync(self.store, fb, fill_wait_s=0).sync(self.NOW)
        self.assertEqual(fb.sent, [])

    def test_reversal_closes_then_opens(self):
        self.add("REV")
        self.tranche("REV", "EMA5_10", "short", 80)
        fb = FakeBroker(positions={"REV": 100})
        BrokerSync(self.store, fb, fill_wait_s=0).sync(self.NOW)
        self.assertEqual(fb.sent, [("REV", "sell", 100), ("REV", "sell", 80)])
        self.assertEqual(fb.pos["REV"], -80)

    def test_working_order_blocks_duplicates(self):
        self.add("SLO")
        self.tranche("SLO", "EMA5_10", "long", 40)
        fb = FakeBroker(hold=True)
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        sync.sync(self.NOW)
        sync.sync(self.NOW)
        self.assertEqual(fb.sent, [("SLO", "buy", 40)])
        self.assertTrue(sync.needs_followup)

    def test_rejection_is_recorded(self):
        self.add("HTB")
        self.tranche("HTB", "VWAP", "short", 25)
        fb = FakeBroker(reject={"HTB"})
        BrokerSync(self.store, fb, fill_wait_s=0).sync(self.NOW)
        o = self.store.q("SELECT * FROM orders")[0]
        self.assertEqual(o["status"], "rejected")
        self.assertIn("not shortable", o["message"])

    def test_removed_symbol_is_closed_at_paper(self):
        sid = self.add("BYE")
        fb = FakeBroker(positions={"BYE": -60})
        self.store.x("UPDATE symbols SET status='removed' WHERE id=?", (sid,))
        BrokerSync(self.store, fb, fill_wait_s=0).sync(self.NOW)
        self.assertEqual(fb.sent, [("BYE", "buy", 60)])

    def test_status_reports_mismatch(self):
        self.add("MIS")
        self.tranche("MIS", "EMA5_10", "long", 10)
        st = BrokerSync(self.store, FakeBroker()).status()
        self.assertEqual(st["reconciliation"][0]["model"], 10)
        self.assertFalse(st["reconciliation"][0]["match"])

    def test_sync_setting_is_boolean(self):
        self.assertFalse(self.store.settings()["broker_sync_enabled"])
        self.assertTrue(self.eng.update_settings({"broker_sync_enabled": True})["broker_sync_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
