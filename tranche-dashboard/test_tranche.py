"""Offline tests: indicator math, the Russo VWAP-fail trigger, and the engine's
entries, sizing, stops, borrow fees and gating. Run: python test_tranche.py"""

import math
import os
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, time, timedelta

from broker import AlpacaPaper, BrokerError, BrokerSync, LiveSync
import app
import review
from app import bar_for_boundary, boundaries
from engine import LIVE_SLEEVES, Engine, ValidationError, _dt, vwap_fail
from indicators import (ET, Bar, atr, bar_bucket, cross_adverse_moves, crossed_below,
                        ema, ema_cross, percentile,
                        price_tick, resample,
                        resample_hourly, session_bar_ends, session_hour_ends,
                        session_vwap)
from store import Store

FIVE = timedelta(minutes=5)


def hour_bars(day: date, hour_idx: int, o, h, l, c, vol=1000.0) -> list[Bar]:
    """5-minute bars forming one clock-aligned hourly bar: index 0 is the
    9:30-10:00 half hour (6 bars), index k>=1 is (9+k):00-(10+k):00 (12 bars)."""
    if hour_idx == 0:
        start, n = datetime.combine(day, time(9, 30), tzinfo=ET), 6
    else:
        start, n = datetime.combine(day, time(9 + hour_idx, 0), tzinfo=ET), 12
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

    def session_open(self, symbol, day, now):
        for b in self.bars:  # the 9:30 print exists before its 5-minute bar closes
            if b.session == day and b.start <= now:
                return b.open
        return None


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
        # hour 0 is the 9:30-10:00 half hour (6 bars), hour 1 is 10-11 (12 bars)
        self.assertAlmostEqual(session_vwap(bars, d, end1), (10 * 600 + 20 * 3600) / 4200)

    def test_atr_and_cross(self):
        d = date(2026, 9, 21)
        hourly = resample_hourly(day_from_closes(d, [10] * 7) + day_from_closes(d + timedelta(1), [10] * 7))
        a = atr(hourly, 3)
        self.assertIsNotNone(a[3])
        self.assertTrue(crossed_below(2, 1, 0.5, 1))
        self.assertFalse(crossed_below(0.5, 1, 0.4, 1))


class FifteenMinuteTests(unittest.TestCase):
    d = date(2026, 9, 21)

    def test_buckets_and_session_ends(self):
        at = lambda h, m: datetime.combine(self.d, time(h, m), tzinfo=ET)
        self.assertEqual(bar_bucket(at(9, 40), 15), (at(9, 30), at(9, 45)))
        self.assertEqual(bar_bucket(at(15, 55), 15), (at(15, 45), at(16, 0)))
        self.assertEqual(bar_bucket(at(9, 40), 60), (at(9, 30), at(10, 0)))  # hourly unchanged
        ends = session_bar_ends(self.d, 15)
        self.assertEqual(len(ends), 26)
        self.assertEqual((ends[0], ends[-1]), (at(9, 45), at(16, 0)))

    def test_resample_15(self):
        bars = day_from_closes(self.d, [10, 11, 12, 13, 14, 15, 16])
        q = resample(bars, 15)
        self.assertEqual(len(q), 26)
        self.assertEqual(q[0].open, 10)
        self.assertEqual(q[-1].close, 16)
        self.assertEqual(sum(b.volume for b in q), sum(b.volume for b in bars))

    def test_check_schedule_15(self):
        got = [b.strftime("%H:%M") for b in boundaries(self.d, timedelta(minutes=2), 15)]
        self.assertEqual(got[:4], ["09:32", "09:47", "10:02", "10:17"])
        self.assertEqual(got[-2:], ["15:47", "15:57"])  # 15:57 = close-by-end-of-day check
        self.assertEqual(len(got), 27)                  # the 15:45-16:00 bar acts at the next open


class FiveMinuteTests(unittest.TestCase):
    d = date(2026, 9, 21)

    def test_five_minute_bars_are_the_raw_bars(self):
        bars = day_from_closes(self.d, [10, 11, 12, 13, 14, 15, 16])
        q = resample(bars, 5)
        self.assertEqual(len(q), 78)
        self.assertEqual([(b.start, b.end, b.close) for b in q],
                         [(b.start, b.end, b.close) for b in bars])
        at = lambda h, m: datetime.combine(self.d, time(h, m), tzinfo=ET)
        self.assertEqual(bar_bucket(at(9, 37), 5), (at(9, 35), at(9, 40)))

    def test_check_schedule_5(self):
        got = [b.strftime("%H:%M") for b in boundaries(self.d, timedelta(minutes=2), 5)]
        self.assertEqual(got[:3], ["09:32", "09:37", "09:42"])
        self.assertEqual(got[-1], "15:57")          # the 15:55-16:00 bar acts at the next open
        self.assertEqual(len(got), 78)


class EmaCrossTests(unittest.TestCase):
    def test_equal_at_chart_precision_is_not_a_signal(self):
        # 10 EMA 1.537 vs 20 EMA 1.538: both show 1.54 on a chart -> no cross
        self.assertEqual(ema_cross([1.56, 1.537], [1.55, 1.538], 1, 0.01), 0)

    def test_cross_needs_clear_separation(self):
        self.assertEqual(ema_cross([1.56, 1.52], [1.55, 1.54], 1, 0.01), -1)
        self.assertEqual(ema_cross([1.50, 1.56], [1.52, 1.54], 1, 0.01), 1)

    def test_touch_then_separate_counts_once(self):
        a = [1.56, 1.54, 1.54, 1.52, 1.51]   # above, equal, equal, below, below
        b = [1.55, 1.54, 1.54, 1.54, 1.54]
        self.assertEqual([ema_cross(a, b, i, 0.01) for i in range(1, 5)], [0, 0, -1, 0])

    def test_touch_and_bounce_same_side_is_not_a_cross(self):
        a = [1.56, 1.54, 1.57]
        b = [1.55, 1.54, 1.55]
        self.assertEqual(ema_cross(a, b, 2, 0.01), 0)

    def test_aixc_sep24_finviz_case(self):
        # Finviz 15-min AIXC. Bars: ... 3:15, 3:30, 3:45 (last bar Sep 24), 9:30 Sep 25.
        # 3:30 values are backed out from the 3:45 readings (+/- rounding).
        e5 = [1.60, 1.5695, 1.52, 1.56]
        e10 = [1.58, 1.5700, 1.54, 1.56]
        e20 = [1.55, 1.5530, 1.54, 1.55]
        tick = price_tick(1.42)
        # 5/10: the sub-cent dip at 3:30 is "equal" on the chart, 3:45 is the cross
        self.assertEqual([ema_cross(e5, e10, i, tick) for i in (1, 2, 3)], [0, -1, 0])
        # 10/20: equal at 3:45, back above at 9:30 - never a signal
        self.assertEqual([ema_cross(e10, e20, i, tick) for i in (1, 2, 3)], [0, 0, 0])

    def test_sub_dollar_uses_four_decimals(self):
        self.assertEqual(price_tick(0.85), 0.0001)
        self.assertEqual(ema_cross([0.8512, 0.8501], [0.8505, 0.8504], 1, 0.0001), -1)


class EmaSizingTests(unittest.TestCase):
    def test_adverse_moves_per_cross_trade(self):
        # fast/slow lines alternate sides; closes chosen so each trade's worst move is known
        fast = [2, 1, 1, 3, 3, 1, 1, 3]
        slow = [1, 2, 2, 2, 2, 2, 2, 2]
        closes = [10, 10, 11, 10, 9, 10, 12, 12]
        # k=1 cross down (short @10): closes 11 -> worst +10%, closes at k=3 cross up
        # k=3 long @10: 9 -> worst 10%, closes at k=5 cross down
        # k=5 short @10: 12 -> worst 20%, closes at k=7 cross up
        moves = cross_adverse_moves(closes, fast, slow, 7)
        self.assertEqual([round(m, 4) for m in moves], [0.1, 0.1, 0.2])

    def test_percentile(self):
        self.assertEqual(percentile([0.01, 0.02, 0.03, 0.04], 0.75), 0.03)
        self.assertEqual(percentile([0.05], 0.75), 0.05)


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


class FakeFmp:
    """Records calls; answers the stable endpoint unless told it's legacy-only."""

    def __init__(self, rows, legacy_only=False):
        self.rows, self.legacy_only, self.calls = rows, legacy_only, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        if self.legacy_only and "/stable/" in url:
            return FakeResp(200, {"Error Message": "Legacy endpoint only"})
        lo, hi = params["from"], params["to"]
        return FakeResp(200, [r for r in self.rows if lo <= r["date"][:10] <= hi][::-1])


class FmpTests(unittest.TestCase):
    ROWS = [{"date": f"2026-09-24 {h:02d}:{m:02d}:00", "open": 10 + i * 0.1, "high": 10.5 + i * 0.1,
             "low": 9.9 + i * 0.1, "close": 10.2 + i * 0.1, "volume": 1000}
            for i, (h, m) in enumerate([(9, 30), (9, 35), (9, 40), (9, 45)])]

    def setUp(self):
        os.environ["FMP_API_KEY"] = "fmpsecret123"
        os.environ.pop("FMP_BAR_TIME", None)

    def tearDown(self):
        os.environ.pop("FMP_API_KEY", None)
        os.environ.pop("FMP_BAR_TIME", None)

    def provider(self, fake):
        from data import FmpProvider
        return FmpProvider(session=fake)

    def test_parses_new_york_time_as_bar_start(self):
        fake = FakeFmp(self.ROWS)
        now = datetime(2026, 9, 24, 9, 49, tzinfo=ET)          # 9:45-9:50 bar still forming
        bars = self.provider(fake).five_min_bars("AIXC", now)
        self.assertEqual([b.start.strftime("%H:%M") for b in bars], ["09:30", "09:35", "09:40"])
        self.assertEqual(bars[0].open, 10)                       # oldest first
        self.assertTrue(all("/stable/" in u for u, _ in fake.calls))

    def test_falls_back_to_legacy_endpoint(self):
        fake = FakeFmp(self.ROWS, legacy_only=True)
        bars = self.provider(fake).five_min_bars("AIXC", datetime(2026, 9, 24, 10, tzinfo=ET))
        self.assertEqual(len(bars), 4)
        self.assertTrue(any("/api/v3/historical-chart/5min/AIXC" in u for u, _ in fake.calls))

    def test_bar_time_end_setting_shifts_back(self):
        os.environ["FMP_BAR_TIME"] = "end"
        bars = self.provider(FakeFmp(self.ROWS)).five_min_bars("AIXC", datetime(2026, 9, 24, 10, tzinfo=ET))
        self.assertEqual(bars[0].start.strftime("%H:%M"), "09:25")

    def test_second_fetch_is_incremental(self):
        fake = FakeFmp(self.ROWS)
        prov = self.provider(fake)
        prov.five_min_bars("AIXC", datetime(2026, 9, 24, 10, tzinfo=ET))
        first = len(fake.calls)
        prov.five_min_bars("AIXC", datetime(2026, 9, 24, 10, 5, tzinfo=ET))
        self.assertGreater(first, 1)                             # ~35 days in weekly chunks
        self.assertEqual(len(fake.calls) - first, 1)             # then only the last days

    def test_session_open_is_the_930_minute(self):
        rows = [{"date": "2026-09-24 09:29:00", "open": 9.0, "high": 9, "low": 9, "close": 9, "volume": 1},
                {"date": "2026-09-24 09:30:00", "open": 1.54, "high": 1.6, "low": 1.5, "close": 1.55, "volume": 1}]
        price = self.provider(FakeFmp(rows)).session_open("AIXC", date(2026, 9, 24),
                                                        datetime(2026, 9, 24, 9, 32, tzinfo=ET))
        self.assertEqual(price, 1.54)

    def test_errors_never_show_the_key(self):
        class Down:
            def get(self, url, params=None, timeout=None):
                return FakeResp(401, f"bad key {params['apikey']} at {url}?apikey={params['apikey']}")
        with self.assertRaises(Exception) as cm:
            self.provider(Down()).five_min_bars("AIXC", datetime(2026, 9, 24, 10, tzinfo=ET))
        self.assertNotIn("fmpsecret123", str(cm.exception))


class BorrowDefaultMigrationTests(unittest.TestCase):
    def test_old_ten_percent_default_moves_to_250_once(self):
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            st = Store(path)
            st.db.execute("DELETE FROM settings WHERE key='_borrow_default_v2'")
            st.save_settings({"borrow_rate_pct": 10.0})
            st.db.close()
            st = Store(path)
            self.assertEqual(st.settings()["borrow_rate_pct"], 250.0)
            st.save_settings({"borrow_rate_pct": 10.0})   # a deliberate later choice sticks
            st.db.close()
            st = Store(path)
            self.assertEqual(st.settings()["borrow_rate_pct"], 10.0)
            st.db.close()
        finally:
            os.remove(path)


class AuthTests(unittest.TestCase):
    def test_basic_auth(self):
        import base64
        hdr = lambda u, p: "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
        self.assertTrue(app.check_auth(None, None))                 # no password set: open
        self.assertFalse(app.check_auth(None, "s3cret"))
        self.assertFalse(app.check_auth(hdr("x", "wrong"), "s3cret"))
        self.assertTrue(app.check_auth(hdr("anyone", "s3cret"), "s3cret"))
        self.assertTrue(app.check_auth(hdr("me", "pa:ss"), "pa:ss"))  # colon in password
        self.assertFalse(app.check_auth("Basic !!!notbase64", "s3cret"))


class BackupTests(unittest.TestCase):
    class Clock:
        def __init__(self, now):
            self._now = now

        def now(self):
            return self._now

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)
        Engine(self.store, ScriptedProvider([])).add_symbols(
            "BKP", "short_only", 1.0, datetime(2026, 9, 25, 8, tzinfo=ET))

    def tearDown(self):
        import shutil
        self.store.db.close()
        os.remove(self.path)
        shutil.rmtree(self.dir, ignore_errors=True)

    def backups(self, now):
        from backup import Backups
        return Backups({"1h": self.store}, Path(self.dir), self.Clock(now))

    def test_copy_is_a_complete_readable_database(self):
        import sqlite3
        written = self.backups(datetime(2026, 9, 25, 16, 10, tzinfo=ET)).run_backup("daily")
        self.assertEqual([w.name for w in written], ["tranche_1h_2026-09-25.db"])
        rows = sqlite3.connect(written[0]).execute("SELECT symbol FROM symbols").fetchall()
        self.assertEqual(rows, [("BKP",)])
        self.assertFalse(list(Path(self.dir).glob("*.partial")))

    def test_pre_reset_copies_are_timestamped(self):
        w = self.backups(datetime(2026, 9, 25, 11, 42, tzinfo=ET)).run_backup("pre-reset")
        self.assertEqual(w[0].name, "tranche_1h_pre-reset_2026-09-25_1142.db")

    def test_old_daily_copies_are_pruned_but_pre_reset_kept(self):
        old_daily = Path(self.dir) / "tranche_1h_2026-07-01.db"
        old_reset = Path(self.dir) / "tranche_1h_pre-reset_2026-07-01_1000.db"
        old_daily.write_text("x")
        old_reset.write_text("x")
        self.backups(datetime(2026, 9, 25, 16, 10, tzinfo=ET)).run_backup("daily")
        self.assertFalse(old_daily.exists())
        self.assertTrue(old_reset.exists())

    def test_failure_is_reported(self):
        blocker = Path(self.dir) / "file"
        blocker.write_text("x")
        from backup import Backups
        b = Backups({"1h": self.store}, blocker / "sub", self.Clock(datetime(2026, 9, 25, tzinfo=ET)))
        with self.assertRaises(Exception):
            b.run_backup("daily")
        self.assertIn("failed", b.status()["error"])


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

    def test_grade_sets_risk(self):
        eng = self.engine([])
        now = datetime(2026, 9, 21, 8, tzinfo=ET)
        eng.add_symbols("GRA", "short_only", 0.5, now, grade="A+")
        eng.add_symbols("GRC", "short_only", 0.5, now, grade="C")
        eng.add_symbols("GRX", "short_only", 2.5, now, grade="custom")
        got = {r["symbol"]: (r["grade"], r["risk_pct"]) for r in self.store.q("SELECT * FROM symbols")}
        self.assertEqual(got, {"GRA": ("A+", 3.0), "GRC": ("C", 1.0), "GRX": (None, 2.5)})
        eng.set_grade(1, "B", now)
        self.assertEqual(self.store.q("SELECT risk_pct FROM symbols WHERE id=1")[0]["risk_pct"], 1.5)
        with self.assertRaises(ValidationError):
            eng.add_symbols("BAD", "short_only", 1, now, grade="Z")

    def test_choppy_history_sizes_from_past_crosses(self):
        # a sine-wave price gives plenty of past 5/10 crosses -> history-based sizing
        ds = weekdays(date(2026, 8, 3), 12)
        bars, k = [], 0
        for d in ds:
            bars += day_from_closes(d, [50 + 3 * math.sin(2 * math.pi * (k + j) / 10) for j in range(7)])
            k += 7
        eng = self.engine(bars)
        eng.add_symbols("SIN", "long_short", 1.0, datetime.combine(ds[-1], time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(15, 5), tzinfo=ET))   # 5/10 crosses up at 14:00
        t = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")[0]
        msg = self.store.q("SELECT message FROM events WHERE kind='entry' AND sleeve='EMA5_10'")[0]["message"]
        self.assertIn("p75 adverse move of the last", msg)
        hourly = [x for x in resample_hourly(bars) if x.end <= datetime.fromisoformat(t["entry_time"])]
        c = [x.close for x in hourly]
        moves = cross_adverse_moves(c, ema(c, 5), ema(c, 10), len(c) - 1)
        per_share = max(percentile(moves, 0.75), atr(hourly, 14)[-1] / c[-1]) * t["entry_price"]
        self.assertEqual(t["qty"], math.floor(100_000 * 0.01 / 3 / per_share))

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

    def test_ema_cross_short_sizing_and_cross_exit(self):
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
        # a steady uptrend has almost no past crosses, so sizing uses the 2 x ATR fallback
        closes = [b.close for b in hourly]
        self.assertLess(len(cross_adverse_moves(closes, ema(closes, 5), ema(closes, 10),
                                                len(closes) - 1)), 5)
        dist = 2.0 * a
        self.assertEqual(first["qty"], math.floor(100_000 * 0.015 / 3 / dist))
        # no stop: the rip to p+6 does not stop it out; it covers only when the
        # 5 EMA crosses back above the 10, at that bar's close
        self.assertTrue(first["exit_reason"].startswith("5/10 EMA crossed up"), first["exit_reason"])
        exit_bar = [b for b in resample_hourly(bars) if b.end == datetime.fromisoformat(first["exit_time"])][0]
        self.assertAlmostEqual(first["exit_price"], exit_bar.close, places=6)  # slippage is 0 in these tests
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
        expected = 3 * t["qty"] * p * 2.50 / 360  # book default 250%/yr, marked at Friday's close
        self.assertAlmostEqual(t["borrow_fees"], expected, places=6)

    def test_symbol_borrow_rate_overrides_default(self):
        fri, mon = date(2026, 9, 18), date(2026, 9, 21)
        bars, p = [], 50.0
        for d in weekdays(date(2026, 9, 1), 14):
            if d > fri:
                break
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            if d == fri:
                closes = [p - 1, p - 2, p - 3, p - 3.1, p - 3.2, p - 3.3, p - 3.4]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        bars += day_from_closes(mon, [p - 0.1] * 7)
        eng = self.engine(bars)
        eng.add_symbols("OWN", "short_only", 1.0, datetime.combine(fri, time(9), tzinfo=ET))
        eng.set_borrow(1, 150, datetime.combine(fri, time(9), tzinfo=ET))   # under the 200% limit
        eng.tick(datetime.combine(fri, time(16, 5), tzinfo=ET))
        t = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")[0]
        self.assertEqual(t["status"], "open")                               # may hold overnight
        eng.tick(datetime.combine(mon, time(10, 35), tzinfo=ET))
        t = self.store.q("SELECT * FROM tranches WHERE id=?", (t["id"],))[0]
        self.assertAlmostEqual(t["borrow_fees"], 3 * t["qty"] * p * 1.50 / 360, places=6)
        with self.assertRaises(ValidationError):
            eng.set_borrow(1, -5, datetime.combine(mon, time(11), tzinfo=ET))

    def test_overnight_borrow_limit_forces_shorts_flat(self):
        eng, d = self.eod_setup(False)
        eng.set_borrow(1, 260, datetime.combine(d, time(9), tzinfo=ET))      # above the 200% limit
        eng.tick(datetime.combine(d, time(15, 58), tzinfo=ET))
        rows = self.store.q("SELECT * FROM tranches")
        self.assertTrue(rows)
        for t in rows:
            self.assertEqual(t["status"], "closed")
            self.assertIn("above the 200% overnight limit", t["exit_reason"])
            self.assertEqual(t["borrow_fees"], 0)

    def test_default_rate_never_forces_flat(self):
        eng, d = self.eod_setup(False)                    # no own rate: default 250% > 200% limit
        eng.tick(datetime.combine(d, time(15, 58), tzinfo=ET))
        self.assertTrue(self.store.q("SELECT 1 FROM tranches WHERE status='open'"))

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

    def test_vwap_stop_on_new_high_of_day_fills_at_the_stop(self):
        v, d, _ = self.russo_vwap([self.CALM, (49.0, 52.6, 48.9, 50.0)] + [self.CALM] * 3)
        t = v[0]
        self.assertTrue(t["exit_reason"].startswith("stop 52"))
        self.assertAlmostEqual(t["exit_price"], 52.0)          # a stop order at the HOD
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

    def test_vwap_stop_is_not_reset_to_entry_overnight(self):
        # next day trades above the 49.5 entry (50.2) but stays under the
        # entry-day high of 52: still short (the old breakeven reset is gone)
        nxt = [(48.0, 48.5, 47.0, 47.5), (47.5, 50.2, 47.4, 50.0)] + [self.CALM] * 5
        v, _, ds = self.russo_vwap([self.CALM] * 5, next_day=nxt)
        self.assertEqual(v[0]["status"], "open")
        self.assertAlmostEqual(v[0]["stop_price"], 52.0)

    def test_vwap_stop_gap_through_fills_at_open(self):
        nxt = [(53.0, 53.5, 52.8, 53.2)] + [self.CALM] * 6
        v, _, ds = self.russo_vwap([self.CALM] * 5, next_day=nxt)
        t = v[0]
        self.assertIn("gapped through", t["exit_reason"])
        self.assertAlmostEqual(t["exit_price"], 53.0)
        self.assertEqual(t["exit_time"], session_hour_ends(ds[11])[0].isoformat())

    def test_vwap_stop_trails_to_lowest_session_high(self):
        # day 2 highs top out at 50.2; on day 3 the stop is 50.2, not 52
        day2 = [(48.0, 48.5, 47.0, 47.5), (47.5, 50.2, 47.4, 50.0)] + [self.CALM] * 5
        day3 = [(49.8, 50.4, 49.6, 50.1)] + [self.CALM] * 6
        v, ds = self.vwap_multi([45.0] * 10, [day2, day3])
        t = v[0]
        self.assertTrue(t["exit_reason"].startswith("stop 50.2"))
        self.assertAlmostEqual(t["exit_price"], 50.2)
        self.assertEqual(t["exit_time"], session_hour_ends(ds[-1])[0].isoformat())

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

    def vwap_multi(self, prefix, later_days, settings=None):
        """`prefix` = daily closes of the flat sessions before the entry day;
        then the VWAP-fail entry day (short 49.5, HOD 52, calm afterwards) and
        `later_days` (lists of 7 hourly specs). Ticks after the last session."""
        for table in ("tranches", "symbols", "events"):
            self.store.x(f"DELETE FROM {table}")               # fresh book per call
        n = len(prefix) + 1 + len(later_days)
        ds = weekdays(date(2026, 6, 1), n)
        bars = []
        for d, c in zip(ds, prefix):
            bars += day_from_closes(d, [c] * 7)
        d = ds[len(prefix)]
        bars += hour_bars(d, 0, 48.0, 52.0, 47.8, 51.5)
        bars += hour_bars(d, 1, 51.5, 51.8, 49.4, 49.5)
        for k in range(2, 7):
            bars += hour_bars(d, k, 49.0, 49.2, 48.9, 49.0)
        for day, specs in zip(ds[len(prefix) + 1:], later_days):
            for k, spec in enumerate(specs):
                bars += hour_bars(day, k, *spec)
        eng = self.engine(bars)
        if settings:
            eng.update_settings(settings)
        eng.add_symbols("VWM", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(datetime.combine(ds[-1], time(16, 5), tzinfo=ET))
        return self.store.q("SELECT * FROM tranches WHERE sleeve='VWAP' ORDER BY id"), ds

    STILL = (49.0, 49.2, 48.9, 49.0)

    def test_vwap_half_at_10ma_rest_at_20ma(self):
        # 10 sessions at 40 then 10 at 45: the 20-MA sits below the 10-MA
        touch10 = [(48.0, 48.5, 47.0, 47.5), (47.5, 47.6, 45.2, 45.6)] + [(45.6, 45.8, 45.3, 45.6)] * 5
        v, ds = self.vwap_multi([40.0] * 10 + [45.0] * 10, [touch10])
        ma10 = (9 * 45.0 + 49.0) / 10
        ma20 = (9 * 40.0 + 10 * 45.0 + 49.0) / 20
        self.assertEqual(len(v), 2)
        half, rest = v[1], v[0]                               # the split-off half is the new row
        self.assertTrue(half["exit_reason"].startswith("target: daily 10-MA"))
        self.assertAlmostEqual(half["exit_price"], ma10)
        self.assertEqual(rest["status"], "open")
        self.assertEqual(rest["scaled"], 1)
        self.assertAlmostEqual(rest["target_price"], ma20)
        self.assertIn(rest["qty"] - half["qty"], (0, 1))
        total_risk = half["risk_dollars"] + rest["risk_dollars"]
        self.assertAlmostEqual(total_risk, (half["qty"] + rest["qty"]) * 2.5)
        # the next session reaches the 20-MA: the rest covers there
        touch20 = [(45.5, 45.6, 42.0, 42.5)] + [(42.5, 42.8, 42.3, 42.5)] * 6
        v, ds = self.vwap_multi([40.0] * 10 + [45.0] * 10, [touch10, touch20])
        rest = v[0]
        self.assertTrue(rest["exit_reason"].startswith("target 2: daily 20-MA"))
        self.assertEqual(rest["exit_time"], session_hour_ends(ds[-1])[0].isoformat())

    def test_vwap_scale_out_off_covers_all_at_10ma(self):
        touch10 = [(48.0, 48.5, 47.0, 47.5), (47.5, 47.6, 45.2, 45.6)] + [(45.6, 45.8, 45.3, 45.6)] * 5
        v, _ = self.vwap_multi([40.0] * 10 + [45.0] * 10, [touch10], {"vwap_scale_out_20": 0})
        v = [t for t in v if t["trigger"] == "fail"]      # the day also re-enters on an opening fade
        self.assertEqual(len(v), 1)
        self.assertTrue(v[0]["exit_reason"].startswith("target: daily 10-MA"))

    def test_vwap_time_stop_at_last_check_of_10th_session(self):
        drift = [[(49 - 0.02 * k, 49.2 - 0.02 * k, 48.9 - 0.02 * k, 49 - 0.02 * k)] * 7
                 for k in range(1, 10)]                 # each day's high below the last
        v, ds = self.vwap_multi([45.0] * 10, drift)
        t = v[0]
        self.assertTrue(t["exit_reason"].startswith("time stop: held 10 sessions"))
        self.assertEqual(t["exit_time"], datetime.combine(ds[-1], time(15), tzinfo=ET).isoformat())
        self.assertAlmostEqual(t["exit_price"], 49 - 0.02 * 9)
        v, _ = self.vwap_multi([45.0] * 10, drift, {"vwap_max_sessions": 0})
        self.assertEqual(v[0]["status"], "open")               # off: still held

    def fade_day(self, bar1, bar2, rest=((48.8, 49.0, 48.6, 48.8),) * 5, above_bar1=False):
        """10 flat sessions at 50, then a day whose first two (hourly) bars are
        `bar1` and `bar2`. Ticks after the close; returns the VWAP tranches."""
        for table in ("tranches", "symbols", "events"):
            self.store.x(f"DELETE FROM {table}")
        ds = weekdays(date(2026, 6, 1), 11)
        bars = []
        for d in ds[:10]:
            bars += day_from_closes(d, [50.0] * 7)
        d = ds[10]
        if above_bar1:  # red (50.0 -> 49.8) but most volume traded near 48: closes above VWAP
            t0 = datetime.combine(d, time(9, 30), tzinfo=ET)
            bars.append(Bar(t0, t0 + FIVE, 50.0, 50.5, 48.0, 48.2, 5000.0))
            bars += [Bar(t0 + k * FIVE, t0 + (k + 1) * FIVE, 48.2 if k == 1 else 49.8, 49.9,
                         48.2, 49.8, 100.0) for k in range(1, 6)]
        else:
            bars += hour_bars(d, 0, *bar1)
        bars += hour_bars(d, 1, *bar2)
        for k, spec in enumerate(rest, start=2):
            bars += hour_bars(d, k, *spec)
        eng = self.engine(bars)
        eng.add_symbols("FAD", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(datetime.combine(d, time(16, 5), tzinfo=ET))
        return self.store.q("SELECT * FROM tranches WHERE sleeve='VWAP' ORDER BY id"), d

    RED1 = (50.0, 50.5, 49.0, 49.2)       # red, closes under its VWAP (~49.3)
    RED2 = (49.2, 49.8, 48.5, 48.8)       # red, under VWAP, lower high than 50.5

    def test_opening_fade_fires_on_second_bar(self):
        v, d = self.fade_day(self.RED1, self.RED2)
        self.assertEqual(len(v), 1)
        t = v[0]
        self.assertEqual(t["trigger"], "open_fade")
        self.assertEqual(t["entry_time"], session_hour_ends(d)[1].isoformat())   # 11:00 close
        self.assertAlmostEqual(t["entry_price"], 48.8)
        self.assertAlmostEqual(t["stop_price"], 50.5)                           # HOD at entry
        ev = self.store.q("SELECT message FROM events WHERE kind='entry'")[0]["message"]
        self.assertIn("opening fade", ev)

    def test_opening_fade_needs_both_bars_red_below_vwap_and_lower_high(self):
        green1 = (48.8, 50.5, 48.7, 49.2)                 # bar 1 green
        self.assertFalse(self.fade_day(green1, self.RED2)[0])
        green2 = (48.5, 49.8, 48.4, 48.8)                 # bar 2 green
        self.assertFalse(self.fade_day(self.RED1, green2)[0])
        new_high = (49.2, 50.7, 48.5, 48.8)               # bar 2 makes a new high of day
        self.assertFalse(self.fade_day(self.RED1, new_high)[0])
        # bar 1 closed above VWAP: not an opening fade - the standard Trigger B takes it
        v, _ = self.fade_day(self.RED1, self.RED2, above_bar1=True)
        self.assertEqual([t["trigger"] for t in v], ["fail"])

    def test_executions_export_open_and_close_legs(self):
        v, d = self.fade_day(self.RED1, self.RED2)          # one short, opened and closed
        eng = Engine(self.store, ScriptedProvider([]))
        rows = eng.executions()
        self.assertEqual([r["Type"] for r in rows], ["Open", "Close"])
        o, c = rows
        self.assertEqual((o["Symbol"], o["Action"], o["Direction"], o["Quantity"]),
                         ("FAD", "SELL", "Short", v[0]["qty"]))
        self.assertEqual((c["Action"], c["Direction"], c["Quantity"]), ("BUY", "Short", v[0]["qty"]))
        self.assertEqual((o["Date"], o["Time"]), (d.isoformat(), "11:00:00"))
        self.assertAlmostEqual(o["Price"], v[0]["entry_price"])
        self.assertAlmostEqual(c["Price"], v[0]["exit_price"])
        self.assertIn("opening fade", o["Tranche"])
        # date filter is on the opening date; open trades only when asked
        self.assertEqual(eng.executions(start=d + timedelta(days=1)), [])
        self.store.x("UPDATE tranches SET status='open', exit_time=NULL, exit_price=NULL")
        self.assertEqual(eng.executions(), [])
        self.assertEqual([r["Type"] for r in eng.executions(include_open=True)], ["Open"])

    def last_bar_cross(self):
        """Uptrend, then the 15:00-16:00 bar drops hard enough to cross the
        5 EMA below the 10 EMA. The next session opens at 47.0."""
        ds = weekdays(date(2026, 8, 3), 10)
        bars, p = [], 50.0
        for d in ds[:-1]:
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            if d == ds[-2]:
                closes[-1] = closes[-2] - 3.0
            bars += day_from_closes(d, closes)
            p = closes[-1]
        bars += day_from_closes(ds[-1], [47.0] * 7)
        return bars, ds[-2], ds[-1]

    def test_final_bar_signal_executes_at_next_open(self):
        bars, d, nxt = self.last_bar_cross()
        eng = self.engine(bars)
        eng.add_symbols("OPN", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(datetime.combine(d, time(16, 5), tzinfo=ET))
        q = "SELECT * FROM tranches WHERE sleeve='EMA5_10'"
        self.assertFalse(self.store.q(q))                      # nothing at 16:00
        eng.tick(datetime.combine(nxt, time(9, 32), tzinfo=ET))
        t = self.store.q(q)[0]
        self.assertEqual(t["side"], "short")
        self.assertEqual(t["entry_time"], datetime.combine(nxt, time(9, 30), tzinfo=ET).isoformat())
        self.assertAlmostEqual(t["entry_price"], 47.0)         # the opening print

    def test_vwap_fail_on_last_bar_is_not_traded_at_open(self):
        ds = weekdays(date(2026, 8, 3), 12)
        bars = []
        for d in ds[:10]:
            bars += day_from_closes(d, [45.0] * 7)
        d, nxt = ds[10], ds[11]
        bars += hour_bars(d, 0, 48.0, 52.0, 47.8, 51.5)          # closes above VWAP, HOD 52
        for k in range(1, 6):
            bars += hour_bars(d, k, 51.5, 51.8, 51.2, 51.5)
        bars += hour_bars(d, 6, 51.5, 51.6, 49.0, 49.2)           # VWAP fail on the LAST bar
        bars += day_from_closes(nxt, [49.0] * 7)
        eng = self.engine(bars)
        eng.add_symbols("VCL", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng.tick(datetime.combine(nxt, time(9, 32), tzinfo=ET))
        self.assertFalse(self.store.q("SELECT 1 FROM tranches WHERE sleeve='VWAP'"))
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE sleeve='VWAP' AND kind='skip' "
                                     "AND message LIKE '%resets overnight%'"))

    def test_bar_table_shows_the_signal_and_when_it_acts(self):
        bars, d, nxt = self.last_bar_cross()
        eng = self.engine(bars)
        rows = eng.bar_table("TBL", datetime.combine(nxt, time(9, 32), tzinfo=ET))
        last = rows[-1]
        self.assertTrue(last["bar_start_et"].endswith("15:00"))
        self.assertIn("5/10 cross DOWN", last["signals"])
        self.assertEqual(last["acts_at"], "next session 9:30 open (EMA only)")
        self.assertIsNotNone(last["ema5"])

    def test_entry_log_explains_the_numbers(self):
        bars, d, nxt = self.last_bar_cross()
        eng = self.engine(bars)
        eng.add_symbols("LOG", "short_only", 1.0, datetime.combine(nxt, time(8), tzinfo=ET))
        eng.tick(datetime.combine(nxt, time(9, 32), tzinfo=ET))
        msg = self.store.q("SELECT message FROM events WHERE kind='entry' AND sleeve='EMA5_10'")[0]["message"]
        for part in ("EMA5", "EMA10", "15:00-16:00 bar", "9:30 open 47", "x ATR", "budget"):
            self.assertIn(part, msg)

    def test_premarket_add_catches_yesterdays_last_bar(self):
        bars, d, nxt = self.last_bar_cross()
        eng = self.engine(bars)
        eng.add_symbols("PRE", "short_only", 1.0, datetime.combine(nxt, time(8), tzinfo=ET))
        eng.tick(datetime.combine(nxt, time(9, 32), tzinfo=ET))
        self.assertTrue(self.store.q("SELECT 1 FROM tranches WHERE sleeve='EMA5_10'"))

    def test_add_after_open_skips_yesterdays_last_bar(self):
        bars, d, nxt = self.last_bar_cross()
        eng = self.engine(bars)
        eng.add_symbols("LTE", "short_only", 1.0, datetime.combine(nxt, time(9, 40), tzinfo=ET))
        eng.tick(datetime.combine(nxt, time(10, 2), tzinfo=ET))
        self.assertFalse(self.store.q("SELECT 1 FROM tranches WHERE sleeve='EMA5_10'"))

    def test_check_schedule(self):
        d = date(2026, 9, 21)  # Monday
        got = [b.strftime("%H:%M") for b in boundaries(d, timedelta(minutes=2))]
        self.assertEqual(got, ["09:32", "10:02", "11:02", "12:02", "13:02", "14:02", "15:02", "15:57"])
        self.assertEqual(boundaries(date(2026, 9, 19), timedelta(0)), [])   # Saturday
        end, acts = bar_for_boundary(datetime.combine(d, time(9, 32), tzinfo=ET), timedelta(minutes=2))
        self.assertEqual(end, datetime.combine(date(2026, 9, 18), time(16), tzinfo=ET))  # Friday close
        self.assertEqual(acts, datetime.combine(d, time(9, 30), tzinfo=ET))

    def test_15m_book_trades_on_15m_bars(self):
        # steady uptrend, then a sharp drop inside the 10:00-10:15 bar: the
        # 15-minute book shorts at 10:15; the hourly book can't act before 11:00
        ds = weekdays(date(2026, 8, 3), 6)
        bars, p = [], 50.0
        for d in ds[:-1]:
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        d = ds[-1]
        bars += hour_bars(d, 0, p, p + 0.3, p - 0.1, p + 0.2)
        t = datetime.combine(d, time(10, 0), tzinfo=ET)
        for k, c in enumerate([p - 1.5, p - 1.6, p - 1.7]):  # 10:00-10:15 selloff
            bars.append(Bar(t + k * FIVE, t + (k + 1) * FIVE, c + 0.5, c + 0.6, c - 0.1, c, 1000.0))
        eng15 = Engine(self.store, ScriptedProvider(bars), minutes=15)
        eng15.add_symbols("QTR", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng15.tick(datetime.combine(d, time(10, 17), tzinfo=ET))
        t15 = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")
        self.assertEqual(len(t15), 1)
        self.assertEqual(t15[0]["entry_time"], datetime.combine(d, time(10, 15), tzinfo=ET).isoformat())
        self.assertAlmostEqual(t15[0]["entry_price"], p - 1.7)

    def test_5m_book_trades_on_5m_bars(self):
        # steady uptrend, then a drop through the 10:00-10:05 bar: the 5-minute
        # book shorts at 10:05, before the 15-minute book could act (10:15)
        ds = weekdays(date(2026, 8, 3), 6)
        bars, p = [], 50.0
        for d in ds[:-1]:
            closes = [p + 0.1 * (k + 1) for k in range(7)]
            bars += day_from_closes(d, closes)
            p = closes[-1]
        d = ds[-1]
        bars += hour_bars(d, 0, p, p + 0.3, p - 0.1, p + 0.2)
        t = datetime.combine(d, time(10, 0), tzinfo=ET)
        bars.append(Bar(t, t + FIVE, p, p + 0.1, p - 3.1, p - 3.0, 1000.0))
        eng5 = Engine(self.store, ScriptedProvider(bars), minutes=5)
        eng5.add_symbols("FIV", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        eng5.tick(datetime.combine(d, time(10, 7), tzinfo=ET))
        t5 = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")
        self.assertEqual(len(t5), 1)
        self.assertEqual(t5[0]["entry_time"], datetime.combine(d, time(10, 5), tzinfo=ET).isoformat())
        self.assertAlmostEqual(t5[0]["entry_price"], p - 3.0)

    def test_behind_reports_unprocessed_hour(self):
        d = weekdays(date(2026, 8, 3), 1)[0]
        bars = day_from_closes(d, [50.0] * 7)
        cut = datetime.combine(d, time(9, 45), tzinfo=ET)    # delayed feed
        feed = ScriptedProvider([b for b in bars if b.end <= cut])
        eng = Engine(self.store, feed)
        eng.add_symbols("DLY", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        hour_end = session_hour_ends(d)[0]
        eng.tick(datetime.combine(d, time(10, 2), tzinfo=ET))
        self.assertTrue(eng.behind(hour_end))                 # still waiting
        feed.bars = bars                                        # data arrives
        eng.tick(datetime.combine(d, time(10, 17), tzinfo=ET))
        self.assertFalse(eng.behind(hour_end))

    def test_delayed_feed_does_not_close_partial_hour(self):
        d = weekdays(date(2026, 8, 3), 1)[0]
        bars = day_from_closes(d, [50.0] * 7)
        cut = datetime.combine(d, time(9, 45), tzinfo=ET)    # feed lags: data to 9:45 only
        eng = self.engine([b for b in bars if b.end <= cut])
        eng.add_symbols("DLY", "short_only", 1.0, datetime.combine(d, time(9), tzinfo=ET))
        with self.assertRaises(RuntimeError):
            eng._process_symbol(self.store.q("SELECT * FROM symbols")[0],
                                datetime.combine(d, time(10, 2), tzinfo=ET), self.store.settings())

    def eod_setup(self, eod: bool):
        """Uptrend, then a day that falls all session: both EMA tranches go short early."""
        bars, ds = self.uptrend_then_drop(8, lambda p: [p - 1.0, p - 2.5, p - 4, p - 5, p - 6, p - 7, p - 8])
        eng = self.engine(bars)
        eng.add_symbols("DAY", "long_short", 1.0, datetime.combine(ds[-1], time(9), tzinfo=ET),
                        eod_close=eod)
        return eng, ds[-1]

    def test_eod_close_flattens_at_1555(self):
        eng, d = self.eod_setup(False)
        eng.tick(datetime.combine(d, time(15, 58), tzinfo=ET))
        held = self.store.q("SELECT * FROM tranches WHERE status='open'")
        self.assertTrue(held)                                  # without the setting: held
        self.store.x("DELETE FROM tranches"); self.store.x("DELETE FROM symbols")
        eng, d = self.eod_setup(True)
        eng.tick(datetime.combine(d, time(15, 54), tzinfo=ET))  # before 15:55 (delay is 0 here)
        self.assertTrue(self.store.q("SELECT 1 FROM tranches WHERE status='open'"))
        eng.tick(datetime.combine(d, time(15, 58), tzinfo=ET))
        self.assertFalse(self.store.q("SELECT 1 FROM tranches WHERE status='open'"))
        closed = self.store.q("SELECT * FROM tranches")
        self.assertEqual(len(closed), len(held))               # it traded the same trades
        five = eng.provider.five_min_bars("DAY", datetime.combine(d, time(16), tzinfo=ET))
        px = next(b.close for b in five if b.end == datetime.combine(d, time(15, 55), tzinfo=ET))
        for t in closed:
            self.assertIn("close by end of day", t["exit_reason"])
            self.assertEqual(_dt(t["exit_time"]), datetime.combine(d, time(15, 55), tzinfo=ET))
            self.assertAlmostEqual(t["exit_price"], px)
            self.assertEqual(t["borrow_fees"], 0)              # never held overnight

    def test_eod_close_catches_up_after_an_outage(self):
        eng, d = self.eod_setup(True)
        eng.tick(datetime.combine(d, time(13, 5), tzinfo=ET))
        self.assertTrue(self.store.q("SELECT 1 FROM tranches WHERE status='open'"))
        eng.tick(datetime.combine(d + timedelta(days=3), time(10, 5), tzinfo=ET))  # down until Mon
        for t in self.store.q("SELECT * FROM tranches"):
            self.assertEqual(t["status"], "closed")
            self.assertEqual(_dt(t["exit_time"]), datetime.combine(d, time(15, 55), tzinfo=ET))
            self.assertEqual(t["borrow_fees"], 0)

    def test_eod_close_blocks_entries_from_1555(self):
        from engine import Fill
        eng = self.engine([])
        now = datetime(2026, 9, 21, 15, 55, tzinfo=ET)
        eng.add_symbols("LATE", "long_short", 1.0, now - timedelta(hours=6), eod_close=True)
        sym = self.store.q("SELECT * FROM symbols")[0]
        eng._enter(sym, "VWAP", "short", Fill(10.0, now, now.date(), "close"), None,
                   self.store.settings(), "test", stop_level=11.0)
        self.assertFalse(self.store.q("SELECT 1 FROM tranches"))
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE kind='skip' AND message LIKE '%end of day%'"))
        eng._enter(sym, "VWAP", "short", Fill(10.0, now - timedelta(minutes=5), now.date(), "close"),
                   None, self.store.settings(), "test", stop_level=11.0)
        self.assertEqual(len(self.store.q("SELECT 1 FROM tranches")), 1)  # 15:50 is fine

    def test_eod_check_never_triggers_hourly_retries(self):
        eng = self.engine([])
        eng.add_symbols("RTR", "short_only", 1.0, datetime(2026, 9, 18, 9, tzinfo=ET))
        clock = type("C", (), {"now": lambda self: datetime(2026, 9, 21, 16, 2, tzinfo=ET)})()
        sched = app.Scheduler(eng, clock)
        sched.last_tick = datetime(2026, 9, 21, 15, 57, tzinfo=ET)
        b = datetime(2026, 9, 21, 15, 55, tzinfo=ET)  # delay 0 in these tests
        self.assertFalse(sched.retry_due(datetime(2026, 9, 21, 16, 2, tzinfo=ET), b))

    def test_eod_toggle(self):
        eng = self.engine([])
        now = datetime(2026, 9, 21, 9, tzinfo=ET)
        eng.add_symbols("TOG", "short_only", 1.0, now)
        eng.set_eod_close(1, True, now)
        self.assertEqual(self.store.q("SELECT eod_close FROM symbols")[0]["eod_close"], 1)
        eng.set_eod_close(1, False, now)
        self.assertEqual(self.store.q("SELECT eod_close FROM symbols")[0]["eod_close"], 0)

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
            raise BrokerError(getattr(self, "submit_err", "Alpaca 403: asset not shortable"))
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

    def test_books_never_share_a_paper_account(self):
        env = {"APCA_API_KEY_ID": "PKSAME", "APCA_API_SECRET_KEY": "s1",
               "APCA_15M_API_KEY_ID": "PKSAME", "APCA_15M_API_SECRET_KEY": "s2"}
        old = {k: os.environ.get(k) for k in (*env, "APCA_5M_API_KEY_ID", "APCA_5M_API_SECRET_KEY")}
        for k in ("APCA_5M_API_KEY_ID", "APCA_5M_API_SECRET_KEY"):
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            links = app.link_papers(demo=False)
            self.assertIsNotNone(links["1h"][0])
            self.assertIsNone(links["15m"][0])
            self.assertIn("separate Alpaca paper account", links["15m"][1])
            os.environ["APCA_15M_API_KEY_ID"] = "PKOTHER"
            self.assertIsNotNone(app.link_papers(demo=False)["15m"][0])
            os.environ.update({"APCA_5M_API_KEY_ID": "PKOTHER", "APCA_5M_API_SECRET_KEY": "s3"})
            links = app.link_papers(demo=False)
            self.assertIsNotNone(links["15m"][0])
            self.assertIsNone(links["5m"][0])
            self.assertIn("same as the 15-min book's", links["5m"][1])
            os.environ["APCA_5M_API_KEY_ID"] = "PKTHIRD"
            self.assertTrue(all(v[0] for v in app.link_papers(demo=False).values()))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

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

    def test_rename_moves_open_tranches_and_keeps_history(self):
        sid = self.add("AIXC")
        self.tranche("AIXC", "EMA5_10", "short", 100)
        self.tranche("AIXC", "VWAP", "short", 30)
        self.store.x("UPDATE tranches SET status='closed', exit_time=? WHERE sleeve='VWAP'",
                     (self.NOW.isoformat(),))
        r = self.eng.rename_symbol(sid, "ffr", self.NOW)
        self.assertEqual(r, {"old": "AIXC", "new": "FFR", "moved": 1})
        rows = {t["status"]: t for t in self.store.q("SELECT * FROM tranches")}
        self.assertEqual((rows["open"]["symbol"], rows["open"]["qty"]), ("FFR", 100))
        self.assertEqual(rows["closed"]["symbol"], "AIXC")   # history keeps the old ticker
        sym = self.store.q("SELECT * FROM symbols WHERE id=?", (sid,))[0]
        self.assertEqual((sym["symbol"], sym["renamed_from"]), ("FFR", "AIXC"))
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE kind='renamed'"))

    def test_rename_with_reverse_split_scales_shares_and_prices(self):
        sid = self.add("RSP")
        self.tranche("RSP", "EMA10_20", "long", 105)
        self.eng.rename_symbol(sid, "RSPN", self.NOW, ratio=10)
        t = self.store.q("SELECT * FROM tranches")[0]
        self.assertEqual(t["qty"], 10)                        # 105 / 10 rounds to 10
        self.assertAlmostEqual(t["entry_price"], 100)
        self.assertAlmostEqual(t["stop_price"], 110)

    def test_rename_validation(self):
        sid = self.add("OLD")
        self.add("TAKEN")
        for new, ratio in (("", 1), ("bad ticker!", 1), ("OLD", 1), ("TAKEN", 1), ("NEW", 0)):
            with self.assertRaises(ValidationError, msg=(new, ratio)):
                self.eng.rename_symbol(sid, new, self.NOW, ratio=ratio if ratio else -5)
        self.store.x("UPDATE symbols SET status='removed' WHERE id=?", (sid,))
        with self.assertRaises(ValidationError):
            self.eng.rename_symbol(sid, "NEW", self.NOW)

    def test_renamed_symbol_waits_while_paper_holds_old_ticker(self):
        sid = self.add("AIXC")
        self.tranche("AIXC", "EMA5_10", "short", 100)
        self.eng.rename_symbol(sid, "FFR", self.NOW)
        fb = FakeBroker(positions={"AIXC": -100})
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        sync.sync(self.NOW)
        self.assertEqual(fb.sent, [])                         # no FFR short, no AIXC cover
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE kind='broker-wait'"))
        fb.pos = {"FFR": -100}                                # Alpaca converted it
        sync.sync(self.NOW)
        self.assertEqual(fb.sent, [])
        fb.pos = {}                                           # or: old ticker closed by hand
        sync.sync(self.NOW)
        self.assertEqual(fb.sent, [("FFR", "sell", 100)])

    def test_short_refusal_is_not_retried_today(self):
        self.add("NOSH")
        self.tranche("NOSH", "EMA5_10", "short", 50)
        fb = FakeBroker(reject={"NOSH"})
        fb.submit_err = "Alpaca 422: asset \"NOSH\" cannot be sold short"
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        for _ in range(4):
            sync.sync(self.NOW)
        self.assertEqual(len(self.store.q("SELECT 1 FROM orders")), 1)   # tried once, not 4x
        skips = self.store.q("SELECT message FROM events WHERE kind='broker-skip'")
        self.assertEqual(len(skips), 1)                                   # logged once
        self.assertIn("can't short", skips[0]["message"])
        self.assertIn("can't short", sync.status()["reconciliation"][0]["note"])
        sync.sync(self.NOW + timedelta(days=1))                           # next session: try again
        self.assertEqual(len(self.store.q("SELECT 1 FROM orders")), 2)

    def test_short_refusal_never_blocks_a_cover_or_long(self):
        self.add("COV")
        self.tranche("COV", "EMA5_10", "short", 50)
        fb = FakeBroker(positions={"COV": -20}, reject={"COV"})
        fb.submit_err = "Alpaca 422: asset \"COV\" cannot be sold short"
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        sync.sync(self.NOW)                      # tries to add 30 more short: refused
        fb.reject = set()
        self.store.x("UPDATE tranches SET status='closed'")
        sync.sync(self.NOW + timedelta(minutes=5))
        self.assertEqual(fb.sent[-1], ("COV", "buy", 20))   # the cover still goes out

    def test_halt_pauses_ten_minutes(self):
        self.add("HLT")
        self.tranche("HLT", "EMA5_10", "long", 10)
        fb = FakeBroker(reject={"HLT"})
        fb.submit_err = "Alpaca 422: market order rejected due to trading halt on symbol: \"HLT\""
        sync = BrokerSync(self.store, fb, fill_wait_s=0)
        sync.sync(self.NOW)
        sync.sync(self.NOW + timedelta(minutes=5))
        self.assertEqual(len(self.store.q("SELECT 1 FROM orders")), 1)
        fb.reject = set()
        sync.sync(self.NOW + timedelta(minutes=11))
        self.assertEqual(fb.sent, [("HLT", "buy", 10)])

    def test_status_reports_mismatch(self):
        self.add("MIS")
        self.tranche("MIS", "EMA5_10", "long", 10)
        st = BrokerSync(self.store, FakeBroker()).status()
        self.assertEqual(st["reconciliation"][0]["model"], 10)
        self.assertFalse(st["reconciliation"][0]["match"])

    def test_sync_setting_is_boolean(self):
        self.assertFalse(self.store.settings()["broker_sync_enabled"])
        self.assertTrue(self.eng.update_settings({"broker_sync_enabled": True})["broker_sync_enabled"])


class ReviewTests(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 11, 32, tzinfo=ET)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.stores = {k: Store(os.path.join(self.dir.name, f"{k}.db")) for k in ("1h", "15m")}

    def tearDown(self):
        for st in self.stores.values():
            st.db.close()
        self.dir.cleanup()

    def books(self):
        return [("1h", "Hourly", self.stores["1h"]), ("15m", "15-min", self.stores["15m"])]

    def trade(self, book, sym, side="short", sleeve="VWAP", entry=10.0, exit_=9.0, qty=100,
              opened=None, closed=None, reason="target: daily 10-MA 9", grade="A", fees=5.0,
              trigger=None):
        st = self.stores[book]
        sid = (st.q("SELECT id FROM symbols WHERE symbol=?", (sym,)) or [{}])[0].get("id")
        if sid is None:
            sid = st.x("INSERT INTO symbols (symbol, mode, risk_pct, added_at, grade) "
                       "VALUES (?, 'long_short', 1, ?, 'C')", (sym, self.NOW.isoformat()))
        opened = opened or self.NOW
        closed = closed or self.NOW + timedelta(hours=2)
        gross = (entry - exit_) * qty * (1 if side == "short" else -1)
        return st.x("""INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
                       entry_price, stop_price, risk_dollars, fee_through, borrow_fees, exit_time,
                       exit_price, exit_reason, gross_pnl, status, grade, trigger)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?,?)""",
                    (sid, sym, sleeve, side, qty, opened.isoformat(), entry, entry + 1, 100.0,
                     "2026-09-21", fees, closed.isoformat(), exit_, reason, gross, grade, trigger))

    def test_exit_types_and_tiers(self):
        self.assertEqual(review.exit_type("manual flatten"), "Manual (flatten/remove)")
        self.assertEqual(review.exit_type("symbol removed"), "Manual (flatten/remove)")
        self.assertEqual(review.exit_type("stop 5.2 (entry-day high, trailed"), "Stop")
        self.assertEqual(review.exit_type("target 2: daily 20-MA 4"), "Target 2 (20-MA)")
        self.assertEqual(review.exit_type("target: daily 10-MA 4 - half covered"), "Target (10-MA)")
        self.assertEqual(review.exit_type("time stop: held 10 sessions"), "Time stop")
        self.assertEqual(review.exit_type("5/10 EMA crossed up (bar 10:30)"), "Opposite cross")
        self.assertEqual(review.cap_tier(40e6), "Nano (<$50M)")
        self.assertEqual(review.cap_tier(None), "Unknown")
        self.assertEqual(review.float_tier(9e6), "Low (<10M)")
        self.assertEqual(review.price_tier(0.8), "under $1")
        self.assertEqual(review.time_bucket(datetime(2026, 9, 21, 10, 32, tzinfo=ET)), "10:30-11:00")
        self.assertEqual(review.sessions_held(date(2026, 9, 18), date(2026, 9, 21)), 2)  # Fri->Mon
        self.assertEqual(review.hold_bucket(30, 1), "Under 1 hour")
        self.assertEqual(review.hold_bucket(300, 1), "Same day")

    def test_trades_from_all_books_with_derived_fields(self):
        self.trade("1h", "AAA", trigger="open_fade")
        self.trade("15m", "BBB", side="long", sleeve="EMA5_10", entry=10, exit_=9, grade=None,
                   reason="5/10 EMA crossed down", closed=self.NOW + timedelta(days=3))
        rows = review.closed_trades(self.books(), {"AAA": {"sector": "Technology", "market_cap": 30e6,
                                                           "float_shares": 4e6}})
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB"])
        a, b = rows
        self.assertEqual((a["book"], a["strategy"]), ("1h", "VWAP fail (Russo) - opening fade"))
        self.assertAlmostEqual(a["net"], 95.0)                  # 100 gross - 5 borrow
        self.assertAlmostEqual(a["r"], 0.95)
        self.assertEqual((a["sector"], a["cap_tier"], a["float_tier"]),
                         ("Technology", "Nano (<$50M)", "Low (<10M)"))
        self.assertEqual((a["grade"], a["grade_at_entry"]), ("A", True))
        self.assertEqual((b["grade"], b["grade_at_entry"]), ("C", False))   # symbol's current grade
        self.assertEqual((b["exit_type"], b["sessions"], b["sector"]), ("Opposite cross", 4, "Unknown"))
        self.assertAlmostEqual(b["net"], -105.0)

    def test_journal_and_day_notes(self):
        tid = self.trade("15m", "TJGC")
        st = self.stores["15m"]
        r = review.save_journal(st, tid, "  chased the open ", "Offering, chased, offering")
        self.assertEqual(r, {"note": "chased the open", "tags": ["offering", "chased"]})
        row = review.closed_trades(self.books(), {})[0]
        self.assertEqual((row["note"], row["tags"]), ("chased the open", ["offering", "chased"]))
        with self.assertRaises(ValidationError):
            review.save_journal(st, tid, "", "bad<tag>")
        with self.assertRaises(ValidationError):
            review.save_journal(st, 9999, "x", "")
        review.save_journal(st, tid, "", "")
        self.assertEqual(st.q("SELECT note, tags FROM tranches")[0], {"note": None, "tags": None})
        hub = self.stores["1h"]
        review.save_day_note(hub, "2026-09-21", "chop day")
        self.assertEqual(review.day_notes(hub), {"2026-09-21": "chop day"})
        review.save_day_note(hub, "2026-09-21", "  ")
        self.assertEqual(review.day_notes(hub), {})
        with self.assertRaises(ValidationError):
            review.save_day_note(hub, "21/09/2026", "x")

    def test_partial_close_keeps_grade_and_journal(self):
        eng = Engine(self.stores["1h"], ScriptedProvider([]))
        tid = self.trade("1h", "PRT")
        st = self.stores["1h"]
        st.x("UPDATE tranches SET status='open', exit_time=NULL, note='n', tags='x' WHERE id=?", (tid,))
        t = st.q("SELECT * FROM tranches WHERE id=?", (tid,))[0]
        eng._partial_close(t, 50, 9.0, self.NOW, "target: half", st.settings())
        part = st.q("SELECT * FROM tranches WHERE id!=?", (tid,))[0]
        self.assertEqual((part["grade"], part["note"], part["tags"]), ("A", "n", "x"))

    def test_entry_records_grade(self):
        st = self.stores["1h"]
        st.save_settings({"slippage_bps": 0.0})
        eng = Engine(st, ScriptedProvider([]))
        eng.add_symbols("GRD", "short_only", 1, self.NOW, grade="A+")
        sym = st.q("SELECT * FROM symbols")[0]
        from engine import Fill
        eng._enter(sym, "VWAP", "short", Fill(10.0, self.NOW, self.NOW.date(), "close"), None,
                   st.settings(), "test", stop_level=11.0)
        self.assertEqual(st.q("SELECT grade FROM tranches")[0]["grade"], "A+")

    def test_profile_cache_fetches_once_and_retries_errors(self):
        calls = []

        class Fetch:
            def fetch(self, s):
                calls.append(s)
                if s == "GONE":
                    raise RuntimeError("FMP has no profile")
                return {"sector": "Healthcare", "industry": "Biotech", "market_cap": 2e8,
                        "float_shares": 3e7}
        cache = review.ProfileCache(self.stores["1h"], Fetch())
        t0 = datetime(2026, 9, 21, tzinfo=ET)
        self.assertEqual(cache.refresh(["AAA", "GONE"], t0), 2)
        self.assertEqual(cache.refresh(["AAA", "GONE"], t0 + timedelta(hours=2)), 0)
        self.assertEqual(cache.refresh(["AAA", "GONE"], t0 + timedelta(days=2)), 1)  # error retried daily
        self.assertEqual(calls, ["AAA", "GONE", "GONE"])
        self.assertEqual(cache.all()["AAA"]["sector"], "Healthcare")
        self.assertIn("no FMP profile", cache.status())

    def test_fmp_profile_parsing(self):
        class Resp:
            def __init__(self, body):
                self.status_code, self.body = 200, body

            def json(self):
                return self.body

        class Http:
            def get(self, url, params=None, timeout=None):
                if "float" in url:
                    return Resp([{"symbol": "AAA", "floatShares": 1234567}])
                return Resp([{"symbol": "AAA", "sector": "Technology", "industry": "Software",
                              "marketCap": 45600000}])
        p = review.FmpProfiles("k", Http()).fetch("AAA")
        self.assertEqual(p, {"sector": "Technology", "industry": "Software",
                             "market_cap": 45600000.0, "float_shares": 1234567.0})

    def test_http_routes(self):
        import json as _json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        tid = self.trade("15m", "AAA")

        class B:
            def __init__(s, key, label, store):
                s.key, s.label = key, label
                s.engine = Engine(store, ScriptedProvider([]))
        books = {k: B(k, l, st) for k, l, st in self.books()}
        old = os.environ.pop("TRANCHE_PASSWORD", None)
        oldkey = os.environ.pop("FMP_API_KEY", None)
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(books, "demo", None))
        finally:
            if old is not None:
                os.environ["TRANCHE_PASSWORD"] = old
            if oldkey is not None:
                os.environ["FMP_API_KEY"] = oldkey
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, method="GET" if body is None else "POST",
                                         data=None if body is None else _json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        try:
            code, page = call("/review")
            self.assertEqual(code, 200)
            self.assertIn(b"Trade Review", page)
            code, body = call("/api/review/trades")
            data = _json.loads(body)
            self.assertEqual([t["symbol"] for t in data["trades"]], ["AAA"])
            self.assertIn("FMP_API_KEY", data["profiles_note"])
            code, body = call("/api/review/journal", {"book": "15m", "id": tid, "note": "x", "tags": "a, b"})
            self.assertEqual((code, _json.loads(body)["tags"]), (200, ["a", "b"]))
            code, _ = call("/api/review/journal", {"book": "nope", "id": tid})
            self.assertEqual(code, 400)
            code, _ = call("/api/review/day", {"day": "2026-09-21", "note": "ok"})
            self.assertEqual(code, 200)
            self.assertEqual(_json.loads(call("/api/review/trades")[1])["day_notes"],
                             {"2026-09-21": "ok"})
        finally:
            srv.shutdown()
            srv.server_close()


class DotenvTest(unittest.TestCase):
    def test_last_duplicate_wins_and_is_reported(self):
        keys = ("APCA_15M_API_KEY_ID", "TRANCHE_TEST_ONLY")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            with tempfile.TemporaryDirectory() as d:
                env = Path(d) / ".env"
                env.write_text("APCA_15M_API_KEY_ID=OLDKEY\nTRANCHE_TEST_ONLY=x\n"
                               "APCA_15M_API_KEY_ID=NEWKEY\n")
                notes = app.load_dotenv(env)
            self.assertEqual(os.environ["APCA_15M_API_KEY_ID"], "NEWKEY")
            dup = [n for n in notes if "set on 2 lines (1, 3)" in n]
            self.assertEqual(len(dup), 1)
            self.assertNotIn("OLDKEY", " ".join(notes))  # values are never printed
            self.assertNotIn("NEWKEY", " ".join(notes))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


# --------------------------------------------------------------------- IBKR
import ibkr
from types import SimpleNamespace as NS


class FakeApi:
    """Stand-in for the ib_async module (contracts and order objects)."""
    @staticmethod
    def Stock(symbol, exchange, currency):
        return NS(symbol=symbol, exchange=exchange, currency=currency, secType="STK")

    @staticmethod
    def LimitOrder(action, qty, price):
        return NS(action=action, totalQuantity=qty, lmtPrice=price, orderType="LMT", orderId=0, orderRef="", account="")

    @staticmethod
    def MarketOrder(action, qty):
        return NS(action=action, totalQuantity=qty, orderType="MKT", orderId=0, orderRef="", account="")


class FakeIB:
    """Just enough of ib_async.IB: orders fill at once unless `hold`, symbols in
    `reject` are refused like IBKR's error 201."""

    def __init__(self, accounts=("DU123",), pos=None, netliq=27000.0, hold=False, reject=(),
                 shortable=3.0, shortable_shares=50000.0, bid=4.0, ask=4.02):
        self.accounts, self.pos, self.netliq = list(accounts), dict(pos or {}), netliq
        self.hold, self.reject = hold, set(reject)
        self.connected, self.connects, self.placed, self._trades = False, 0, [], []
        self.q = dict(shortable=shortable, shortableShares=shortable_shares, bid=bid, ask=ask)
        self.daily = float("nan")
        self.mdtype = None

    def isConnected(self):
        return self.connected

    async def connectAsync(self, host, port, clientId, timeout, account):
        self.connects += 1
        self.connected = True

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, n):
        self.mdtype = n

    async def qualifyContractsAsync(self, c):
        return [None] if c.symbol == "NOPE" else [c]

    def accountValues(self, account):
        return [NS(tag="NetLiquidation", value=str(self.netliq), currency="USD"),
                NS(tag="TotalCashValue", value="27000", currency="USD"),
                NS(tag="BuyingPower", value="54000", currency="USD")]

    def positions(self, account):
        return [NS(contract=FakeApi.Stock(s, "SMART", "USD"), position=q, avgCost=4.0)
                for s, q in self.pos.items()]

    def portfolio(self, account):
        return [NS(contract=FakeApi.Stock(s, "SMART", "USD"), unrealizedPNL=-1.5)
                for s in self.pos]

    def reqMktData(self, c, ticks, snapshot, regulatory):
        nan = float("nan")
        return NS(bid=self.q["bid"], ask=self.q["ask"], last=nan, close=nan,
                  shortable=self.q["shortable"], shortableShares=self.q["shortableShares"])

    def cancelMktData(self, c):
        return True

    def placeOrder(self, c, order):
        order.orderId = len(self._trades) + 1
        self.placed.append((c.symbol, order))
        t = NS(contract=c, order=order, fills=[], log=[],
               orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0.0))
        self._trades.append(t)
        if c.symbol in self.reject:
            t.orderStatus.status = "Cancelled"
            t.log.append(NS(message="Order rejected - reason:The contract is not available "
                                    "for short sale.", errorCode=201))
        elif not self.hold:
            self.fill(t)
        return t

    def fill(self, t):
        q = t.order.totalQuantity
        t.orderStatus.status, t.orderStatus.filled, t.orderStatus.avgFillPrice = "Filled", q, 4.01
        t.fills.append(NS(time=datetime(2026, 10, 6, 15, 0)))
        sym = t.contract.symbol
        self.pos[sym] = self.pos.get(sym, 0) + (q if t.order.action == "BUY" else -q)
        if not self.pos[sym]:
            del self.pos[sym]

    def trades(self):
        return list(self._trades)

    def openTrades(self):
        return [t for t in self._trades if t.orderStatus.status not in ibkr.DONE]

    def cancelOrder(self, order):
        for t in self._trades:
            if t.order is order:
                t.orderStatus.status = "Cancelled"

    def reqPnL(self, account):
        return None

    def pnl(self, account):
        return [NS(dailyPnL=self.daily)]


def ib_cfg(**kw):
    env = {"IBKR_ACCOUNT": "DU123", "IBKR_MODE": "paper"}
    env.update(kw)
    return ibkr.config_from_env(env)


class IBKRConfigTests(unittest.TestCase):
    def test_unset_means_not_linked(self):
        self.assertIsNone(ibkr.config_from_env({}))

    def test_book_defaults_to_5m_and_is_validated(self):
        self.assertEqual(ib_cfg().book, "5m")
        self.assertEqual(ib_cfg(IBKR_BOOK="1h").book, "1h")
        with self.assertRaisesRegex(BrokerError, "IBKR_BOOK"):
            ib_cfg(IBKR_BOOK="2m")

    def test_defaults(self):
        c = ib_cfg()
        self.assertEqual((c.mode, c.port, c.max_shares, c.daily_loss_limit),
                         ("paper", 4002, 1, 100.0))
        live = ib_cfg(IBKR_ACCOUNT="U7654321", IBKR_MODE="live")
        self.assertEqual(live.port, 4001)

    def test_account_id_must_match_mode(self):
        with self.assertRaisesRegex(BrokerError, "not a live account"):
            ib_cfg(IBKR_MODE="live")  # DU = paper
        with self.assertRaisesRegex(BrokerError, "not a paper account"):
            ib_cfg(IBKR_ACCOUNT="U7654321")
        with self.assertRaises(BrokerError):
            ib_cfg(IBKR_MODE="real")

    def test_book_and_numbers_validated(self):
        with self.assertRaises(BrokerError):
            ib_cfg(IBKR_MAX_SHARES="0")
        with self.assertRaises(BrokerError):
            ib_cfg(IBKR_MAX_SHARES="one")
        with self.assertRaisesRegex(BrokerError, "above 0"):
            ib_cfg(IBKR_ACCOUNT="U7654321", IBKR_MODE="live", IBKR_DAILY_LOSS_LIMIT="0")

    def test_limit_price_rounds_through_the_market(self):
        self.assertEqual(ibkr.limit_price("buy", 4.02, 3), 4.15)   # 4.1406 up
        self.assertEqual(ibkr.limit_price("sell", 4.00, 3), 3.88)
        self.assertEqual(ibkr.limit_price("buy", 0.5123, 3), 0.5277)  # 4 decimals below $1


class IBKRBrokerTests(unittest.TestCase):
    def mk(self, **kw):
        cfg_kw = {k: v for k, v in kw.items() if k.startswith("IBKR_")}
        ib_kw = {k: v for k, v in kw.items() if not k.startswith("IBKR_")}
        self.ib = FakeIB(**ib_kw)
        return ibkr.IBKRBroker(ib_cfg(**cfg_kw), ib=self.ib, api=FakeApi)

    def test_connects_lazily_to_the_named_account_only(self):
        b = self.mk(accounts=["DU999"])
        self.assertEqual(self.ib.connects, 0)
        with self.assertRaisesRegex(BrokerError, "not DU123"):
            b.account()
        self.assertFalse(self.ib.connected)
        b2 = self.mk()
        self.assertEqual(b2.account()["equity"], 27000.0)
        self.assertEqual(self.ib.mdtype, 1)

    def test_positions_and_orders_in_alpaca_shape(self):
        b = self.mk(pos={"BRK B": 1, "AAA": -1})
        self.assertEqual(b.positions(), {
            "BRK.B": {"qty": "1", "avg_entry_price": "4.0", "unrealized_pl": "-1.5"},
            "AAA": {"qty": "-1", "avg_entry_price": "4.0", "unrealized_pl": "-1.5"}})
        o = b.submit("CCC", 1, "sell", "td-x")
        self.assertEqual((o["status"], o["filled_qty"], o["client_order_id"]), ("filled", "1.0", "td-x"))
        sym, order = self.ib.placed[0]
        self.assertEqual((sym, order.orderType, order.lmtPrice, order.tif, order.account,
                          order.outsideRth), ("CCC", "LMT", 3.88, "DAY", "DU123", False))
        self.assertEqual(b.get_order(o["id"])["status"], "filled")
        self.assertEqual(b.get_order("999")["status"], "expired")

    def test_share_cap_blocks_growth_but_never_a_close(self):
        b = self.mk(pos={"AAA": -1})
        with self.assertRaisesRegex(BrokerError, "cap is 1"):
            b.submit("AAA", 1, "sell", "c1")  # -1 -> -2
        b.submit("AAA", 1, "buy", "c2")      # the cover is always allowed
        self.assertNotIn("AAA", self.ib.pos)

    def test_short_only_refuses_any_buy_that_opens_a_long(self):
        b = self.mk(pos={"AAA": -1})
        with self.assertRaisesRegex(BrokerError, "short only"):
            b.submit("FLAT", 1, "buy", "c1")   # 0 -> +1
        with self.assertRaisesRegex(BrokerError, "short only"):
            b.submit("AAA", 2, "buy", "c2")    # -1 -> +1 (also within the 1-share cap)
        self.assertEqual(self.ib.placed, [])
        b.submit("AAA", 1, "buy", "c3")        # -1 -> 0
        self.assertEqual(self.ib.pos, {})

    def test_market_order_on_request(self):
        b = self.mk()  # limit orders by default
        b.submit("AAA", 1, "sell", "c1")
        b.submit("AAA", 1, "buy", "c2", order_type="market")
        self.assertEqual([o.orderType for _s, o in self.ib.placed], ["LMT", "MKT"])

    def test_closing_a_large_position_is_allowed(self):
        b = self.mk(pos={"BIG": 3}, IBKR_MAX_ORDER_USD="100000")
        b.submit("BIG", 3, "sell", "c")
        self.assertNotIn("BIG", self.ib.pos)

    def test_order_value_cap_and_missing_price(self):
        b = self.mk(bid=1200.0, ask=1300.0)
        with self.assertRaisesRegex(BrokerError, "IBKR_MAX_ORDER_USD"):
            b.submit("PRICY", 1, "sell", "c")
        b = self.mk(bid=-1, ask=-1)
        with self.assertRaisesRegex(BrokerError, "no price"):
            b.submit("DARK", 1, "sell", "c")
        b.submit("DARK", 1, "sell", "c", ref_price=3.0)  # the model's price is the fallback
        self.assertEqual(self.ib.placed[-1][1].lmtPrice, 2.91)

    def test_market_orders_when_configured(self):
        b = self.mk(IBKR_ORDER_TYPE="market")
        b.submit("AAA", 1, "sell", "c")
        self.assertEqual(self.ib.placed[0][1].orderType, "MKT")
        self.assertEqual(b.reprice_s, 0)

    def test_rejection_raises_with_ibkr_message(self):
        b = self.mk(reject={"HTB"})
        with self.assertRaisesRegex(BrokerError, "not available for short sale"):
            b.submit("HTB", 1, "sell", "c")

    def test_unknown_symbol(self):
        with self.assertRaisesRegex(BrokerError, "not found"):
            self.mk().submit("NOPE", 1, "sell", "c")

    def test_short_check(self):
        self.assertIsNone(self.mk().short_check("AAA", 1))
        self.assertIn("no shares", self.mk(shortable=1.0).short_check("AAA", 1))
        self.assertIn("has 0 shares", self.mk(shortable=2.0, shortable_shares=0.0).short_check("AAA", 1))
        b = self.mk(shortable=float("nan"), shortable_shares=float("nan"))
        b.cfg.quote_wait_s = 0
        self.assertIsNone(b.short_check("AAA", 1))  # no data: IBKR's own locate check decides

    def test_cancel_and_day_pnl(self):
        b = self.mk(hold=True)
        o = b.submit("AAA", 1, "sell", "c")
        self.assertEqual(o["status"], "accepted")
        self.assertEqual(len(b.open_orders()), 1)
        b.cancel(o["id"])
        self.assertEqual(b.open_orders(), [])
        self.assertIsNone(b.day_pnl())
        self.ib.daily = -42.0
        self.assertEqual(b.day_pnl(), -42.0)


class LiveGuardTests(unittest.TestCase):
    """BrokerSync with the IBKR broker: share cap, loss limit, kill switch."""
    NOW = BrokerSyncTests.NOW
    setUp, tearDown = BrokerSyncTests.setUp, BrokerSyncTests.tearDown
    add, tranche = BrokerSyncTests.add, BrokerSyncTests.tranche

    def ibsync(self, **kw):
        self.ib = FakeIB(**kw)
        self.b = ibkr.IBKRBroker(ib_cfg(), ib=self.ib, api=FakeApi)
        return BrokerSync(self.store, self.b, fill_wait_s=0, max_shares=self.b.max_shares,
                          daily_loss_limit=self.b.daily_loss_limit)

    def test_mirrors_direction_capped_at_one_share(self):
        self.add("AAA"); self.add("BBB")
        self.tranche("AAA", "EMA5_10", "short", 150)
        self.tranche("AAA", "VWAP", "short", 50)
        self.tranche("BBB", "EMA10_20", "short", 80)
        sync = self.ibsync()
        sync.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1, "BBB": -1})
        sync.sync(self.NOW)
        self.assertEqual(len(self.ib.placed), 2)
        st = sync.status()
        self.assertTrue(all(r["match"] for r in st["reconciliation"]))
        self.assertEqual((st["venue"], st["live"], st["control"]["max_shares"]), ("IBKR paper", False, 1))

    def test_reversal_at_one_share(self):
        self.add("REV")
        self.tranche("REV", "EMA5_10", "short", 80)
        sync = self.ibsync(pos={"REV": 1})
        sync.sync(self.NOW)
        self.assertEqual([(s, o.action, o.totalQuantity) for s, o in self.ib.placed],
                         [("REV", "SELL", 1), ("REV", "SELL", 1)])
        self.assertEqual(self.ib.pos["REV"], -1)

    def test_no_borrow_skips_the_short_but_not_the_cover(self):
        self.add("HTB")
        self.tranche("HTB", "VWAP", "short", 10)
        sync = self.ibsync(shortable=1.0)
        sync.sync(self.NOW)
        self.assertEqual(self.ib.placed, [])
        self.assertIn("no shares to borrow", sync.status()["reconciliation"][0]["note"])
        skips = self.store.q("SELECT message FROM events WHERE kind='broker-skip'")
        self.assertEqual(len(skips), 1)
        sync.sync(self.NOW)
        self.assertEqual(len(self.store.q("SELECT 1 FROM events WHERE kind='broker-skip'")), 1)
        self.store.x("UPDATE tranches SET status='closed'")
        self.ib.pos["HTB"] = -1  # a short opened earlier still gets covered
        sync.sync(self.NOW)
        self.assertEqual(self.ib.placed[-1][1].action, "BUY")

    def test_ibkr_rejection_holds_back_new_shorts_for_the_day(self):
        self.add("HTB")
        self.tranche("HTB", "VWAP", "short", 10)
        sync = self.ibsync(reject={"HTB"})
        sync.sync(self.NOW)
        sync.sync(self.NOW + timedelta(minutes=15))
        self.assertEqual(len(self.ib.placed), 1)  # not resent
        self.assertIn("can't short it today", sync.blocked(self.NOW)["HTB"])

    def test_daily_loss_limit_flattens_and_halts_for_the_day(self):
        self.add("AAA"); self.add("BBB")
        self.tranche("AAA", "EMA5_10", "short", 100)
        self.tranche("BBB", "EMA5_10", "short", 100)
        sync = self.ibsync(pos={"MANUAL": 5})
        sync.sync(self.NOW)  # day start 27,000
        self.assertEqual(sync.day_start(self.NOW), 27000.0)
        self.ib.netliq = 26950.0
        sync._watched = None
        sync.watch(self.NOW)
        self.assertIsNone(sync.stop_reason(self.NOW))
        self.ib.netliq = 26899.0  # down $101
        sync._watched = None
        sync.watch(self.NOW + timedelta(minutes=1))
        self.assertEqual(sync.stop_reason(self.NOW)[0], "flatten")
        self.assertEqual(self.ib.pos, {"MANUAL": 5})  # only managed symbols are closed
        n = len(self.ib.placed)
        sync.sync(self.NOW + timedelta(hours=1))
        self.assertEqual(len(self.ib.placed), n)  # nothing more today
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE kind='broker-halt'"))
        nxt = self.NOW + timedelta(days=1)
        self.assertIsNone(sync.stop_reason(nxt))
        sync.sync(nxt)  # new day, new start equity, back to mirroring the model
        self.assertEqual(sync.day_start(nxt), 26899.0)
        self.assertEqual(self.ib.pos, {"MANUAL": 5, "AAA": -1, "BBB": -1})

    def test_ibkr_daily_pnl_counts_when_worse(self):
        self.add("AAA")
        sync = self.ibsync()
        sync.sync(self.NOW)
        self.ib.daily = -100.0  # e.g. the app started after the loss happened
        sync.sync(self.NOW)
        self.assertIn("daily loss limit", sync.stop_reason(self.NOW)[1])

    def test_kill_switch_pause_and_flatten_persist(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        sync = self.ibsync()
        sync.set_kill("pause", self.NOW)
        sync.sync(self.NOW)
        self.assertEqual(self.ib.placed, [])
        sync.set_kill(None, self.NOW)
        sync.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1})
        sync.set_kill("flatten", self.NOW)
        again = BrokerSync(self.store, self.b, fill_wait_s=0, max_shares=1, daily_loss_limit=100)
        again.sync(self.NOW)  # a restart keeps the switch
        self.assertEqual(self.ib.pos, {})
        again.sync(self.NOW)
        self.assertEqual(self.ib.pos, {})
        self.assertEqual(again.status()["control"]["kill"], "flatten")
        with self.assertRaises(BrokerError):
            again.set_kill("explode", self.NOW)

    def test_flatten_cancels_working_orders_first(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        sync = self.ibsync(hold=True)
        sync.sync(self.NOW)
        self.assertEqual(len(self.ib.openTrades()), 1)
        sync.set_kill("flatten", self.NOW)
        sync.sync(self.NOW)
        self.assertEqual(self.ib.openTrades(), [])
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE message LIKE 'cancelled working%'"))

    def test_unfilled_limit_is_cancelled_and_repriced(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        sync = self.ibsync(hold=True)
        sync.sync(self.NOW)
        sync.sync(self.NOW + timedelta(seconds=10))
        self.assertEqual(len(self.ib.openTrades()), 1)  # still young: left working
        sync.sync(self.NOW + timedelta(seconds=50))
        self.assertEqual(self.ib.openTrades(), [])      # cancelled
        self.assertTrue(sync.needs_followup)
        self.ib.hold = False
        sync.sync(self.NOW + timedelta(seconds=120))    # the follow-up re-sends at a fresh quote
        self.assertEqual(self.ib.pos, {"AAA": -1})
        self.assertEqual(len(self.ib.placed), 2)

    def test_never_cancels_orders_it_did_not_send(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        sync = self.ibsync(hold=True)
        order = FakeApi.LimitOrder("BUY", 1, 1.0)
        order.account = "DU123"
        manual = self.ib.placeOrder(FakeApi.Stock("AAA", "SMART", "USD"), order)
        sync.set_kill("flatten", self.NOW)
        sync.sync(self.NOW + timedelta(minutes=5))
        self.assertEqual(manual.orderStatus.status, "Submitted")

    def test_adapter_refuses_a_long_even_if_the_sync_asks(self):
        self.add("LNG")
        self.tranche("LNG", "EMA5_10", "long", 100)
        sync = self.ibsync()  # plain BrokerSync: no short-only clamp of its own
        sync.sync(self.NOW)
        self.assertEqual(self.ib.placed, [])
        row = self.store.q("SELECT status, message FROM orders")[0]
        self.assertEqual(row["status"], "rejected")
        self.assertIn("short only", row["message"])

    def test_alpaca_path_is_uncapped(self):
        self.add("AAA")
        self.tranche("AAA", "EMA5_10", "short", 100)
        fb = FakeBroker()
        BrokerSync(self.store, fb, fill_wait_s=0).sync(self.NOW)
        self.assertEqual(fb.sent, [("AAA", "sell", 100)])

    def test_link_live_from_env(self):
        keys = ("IBKR_ACCOUNT", "IBKR_MODE")
        old = {k: os.environ.get(k) for k in keys}
        orig = ibkr.IBKRBroker.__init__
        try:
            ibkr.IBKRBroker.__init__ = lambda self, cfg, **kw: orig(self, cfg, ib=FakeIB(), api=FakeApi)
            for k in keys:
                os.environ.pop(k, None)
            self.assertIsNone(app.link_live(False)[0])
            os.environ.update({"IBKR_ACCOUNT": "DU123", "IBKR_MODE": "paper"})
            self.assertIsInstance(app.link_live(False)[0], ibkr.IBKRBroker)
            self.assertIsNone(app.link_live(True)[0])
            os.environ["IBKR_MODE"] = "live"  # DU account with live mode: refused, not linked
            ib, note = app.link_live(False)
            self.assertIsNone(ib)
            self.assertIn("not a live account", note)
        finally:
            ibkr.IBKRBroker.__init__ = orig
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v



class LiveSelectionTests(unittest.TestCase):
    """The IBKR link's scope: the 5-min book only, the 5/10 EMA tranche only,
    short only, one share, stop covers at market."""
    NOW = datetime(2026, 9, 21, 11, 32, tzinfo=ET)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.stores = {k: Store(f"{self.dir.name}/{k}.db") for k in ("1h", "15m", "5m", "live")}
        self.engines = {k: Engine(self.stores[k], ScriptedProvider([]), m)
                        for k, m in (("1h", 60), ("15m", 15), ("5m", 5))}
        self.ib = FakeIB()
        self.b = ibkr.IBKRBroker(ib_cfg(), ib=self.ib, api=FakeApi)
        labels = {"1h": "Hourly", "15m": "15-min", "5m": "5-min"}
        self.live = LiveSync(self.stores["live"], self.b,
                             {k: (labels[k], self.stores[k]) for k in labels},
                             live_book=self.b.cfg.book, sleeves=LIVE_SLEEVES,
                             fill_wait_s=0, max_shares=1, daily_loss_limit=100)

    def tearDown(self):
        for st in self.stores.values():
            st.db.close()
        self.dir.cleanup()

    def add(self, book, sym):
        self.engines[book].add_symbols(sym, "long_short", 1.0, self.NOW)
        return self.stores[book].q("SELECT id FROM symbols WHERE symbol=?", (sym,))[0]["id"]

    def tranche(self, book, sym, sleeve, side, qty):
        st = self.stores[book]
        sid = st.q("SELECT id FROM symbols WHERE symbol=?", (sym,))[0]["id"]
        return st.x("""INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
                       entry_price, stop_price, risk_dollars, fee_through)
                       VALUES (?,?,?,?,?,?,10,11,100,'2026-09-21')""",
                    (sid, sym, sleeve, side, qty, self.NOW.isoformat()))

    def test_scope_constants(self):
        self.assertEqual(LIVE_SLEEVES, ("EMA5_10",))
        self.assertEqual((self.live.live_book, self.live.book_label), ("5m", "5-min"))

    def test_only_the_5_10_tranche_of_the_live_book_counts(self):
        sid = self.add("5m", "AAA")
        self.tranche("5m", "AAA", "VWAP", "short", 50)
        self.engines["5m"].set_live(sid, ["EMA5_10"], self.NOW)
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.placed, [])  # VWAP short open, 5/10 flat: nothing live
        self.tranche("5m", "AAA", "EMA5_10", "short", 100)
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1})
        # marked live in another book, or for another tranche, straight in the db: ignored
        hid = self.add("1h", "HHH")
        self.tranche("1h", "HHH", "EMA5_10", "short", 10)
        self.stores["1h"].x("UPDATE symbols SET live='EMA5_10' WHERE id=?", (hid,))
        vid = self.add("5m", "VVV")
        self.tranche("5m", "VVV", "VWAP", "short", 10)
        self.stores["5m"].x("UPDATE symbols SET live='VWAP' WHERE id=?", (vid,))
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1})
        self.assertEqual(set(self.live.selections()), {"AAA"})

    def test_short_only_a_long_signal_means_flat(self):
        sid = self.add("5m", "AAA")
        tid = self.tranche("5m", "AAA", "EMA5_10", "short", 100)
        self.engines["5m"].set_live(sid, ["EMA5_10"], self.NOW)
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1})
        self.stores["5m"].x("UPDATE tranches SET status='closed', exit_time=?, exit_price=10, "
                            "exit_reason='5/10 EMA crossed up', gross_pnl=0 WHERE id=?",
                            ((self.NOW + timedelta(minutes=5)).isoformat(), tid))
        self.tranche("5m", "AAA", "EMA5_10", "long", 100)  # reversed long in the model
        self.live.sync(self.NOW + timedelta(minutes=6))
        self.assertEqual(self.ib.pos, {})                    # flat, never +1
        self.assertEqual([o.action for _s, o in self.ib.placed], ["SELL", "BUY"])
        self.assertEqual(self.live.desired(), {"AAA": 0})

    def test_stop_cover_goes_out_at_market_other_covers_at_limit(self):
        sid = self.add("5m", "AAA")
        self.engines["5m"].set_live(sid, ["EMA5_10"], self.NOW)
        for reason, kind in (("stop: 5-min close 10.5 above the day's high 10.2", "MKT"),
                             ("close by end of day (IBKR live symbol): flat at 15:55", "LMT")):
            tid = self.tranche("5m", "AAA", "EMA5_10", "short", 100)
            t0 = self.NOW if kind == "MKT" else self.NOW + timedelta(hours=1)
            self.live.sync(t0)
            self.assertEqual(self.ib.placed[-1][1].orderType, "LMT")  # the entry
            self.stores["5m"].x("UPDATE tranches SET status='closed', exit_time=?, exit_price=10, "
                                "exit_reason=?, gross_pnl=0 WHERE id=?",
                                ((t0 + timedelta(minutes=5)).isoformat(), reason, tid))
            self.live.sync(t0 + timedelta(minutes=7))
            self.assertEqual((self.ib.placed[-1][1].action, self.ib.placed[-1][1].orderType),
                             ("BUY", kind))
            self.assertEqual(self.ib.pos, {})

    def test_switched_off_and_removed_symbols_are_closed(self):
        a = self.add("5m", "AAA")
        b = self.add("5m", "BBB")
        self.tranche("5m", "AAA", "EMA5_10", "short", 10)
        self.tranche("5m", "BBB", "EMA5_10", "short", 10)
        self.engines["5m"].set_live(a, ["EMA5_10"], self.NOW)
        self.engines["5m"].set_live(b, ["EMA5_10"], self.NOW)
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.pos, {"AAA": -1, "BBB": -1})
        self.engines["5m"].set_live(a, [], self.NOW)
        self.stores["5m"].x("UPDATE symbols SET last_price=10 WHERE id=?", (b,))
        self.engines["5m"].set_status(b, "removed", self.NOW)
        self.live.sync(self.NOW)
        self.assertEqual(self.ib.pos, {})

    def test_set_live_refuses_vwap_and_10_20(self):
        a = self.add("5m", "AAA")
        for bad in (["VWAP"], ["EMA10_20"], ["EMA5_10", "VWAP"], ["MACD"]):
            with self.assertRaises(ValidationError):
                self.engines["5m"].set_live(a, bad, self.NOW)
        self.assertIsNone(self.stores["5m"].q("SELECT live FROM symbols")[0]["live"])
        self.assertEqual(self.engines["5m"].set_live(a, ["EMA5_10"], self.NOW), ["EMA5_10"])

    def test_http_live_routes(self):
        import json as _json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        h = self.add("1h", "AAA")
        f = self.add("5m", "AAA")

        class Clock:
            demo = False

            def now(s):
                return self.NOW

        class B:
            def __init__(s, key, label, eng):
                s.key, s.label, s.engine, s.minutes = key, label, eng, eng.minutes
                s.sched = app.Scheduler(eng, Clock(), None, self.live)
                s.link_note = "no paper keys"
        books = {k: B(k, l, self.engines[k]) for k, l in (("1h", "Hourly"), ("15m", "15-min"), ("5m", "5-min"))}
        old = os.environ.pop("TRANCHE_PASSWORD", None)
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(books, "demo", None, None, self.live))
        finally:
            if old is not None:
                os.environ["TRANCHE_PASSWORD"] = old
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body):
            req = urllib.request.Request(base + path, method="POST", data=_json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, _json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, _json.loads(e.read())
        try:
            code, err = call(f"/api/1h/symbols/{h}/live", {"sleeves": ["EMA5_10"]})
            self.assertEqual(code, 400)
            self.assertIn("only in the 5-min book", err["error"])
            code, err = call(f"/api/5m/symbols/{f}/live", {"sleeves": ["VWAP"]})
            self.assertEqual(code, 400)
            self.assertIn("only the 5/10 EMA tranche", err["error"])
            self.assertEqual(call(f"/api/5m/symbols/{f}/live", {"sleeves": ["EMA5_10"]})[0], 200)
            self.assertEqual(self.stores["5m"].q("SELECT live FROM symbols")[0]["live"], "EMA5_10")
            self.assertEqual(call(f"/api/1h/symbols/{h}/live", {"sleeves": []})[0], 200)  # off is fine
            self.assertEqual(call("/api/live/settings", {"enabled": True})[0], 200)
            self.assertTrue(self.live.enabled())
            self.assertEqual(call("/api/live/kill", {"mode": "pause"})[0], 200)
            self.assertEqual(self.live.stop_reason(self.NOW)[0], "pause")
            self.assertEqual(call("/api/live/kill", {"mode": "nuke"})[0], 400)
            self.assertEqual(call("/api/live/kill", {"mode": "off"})[0], 200)
            with urllib.request.urlopen(base + "/api/5m/state") as r:
                st = _json.loads(r.read())
            self.assertEqual((st["live"]["configured"], st["live"]["book"], st["live"]["venue"]),
                             (True, "5m", "IBKR paper"))
        finally:
            srv.shutdown()
            srv.server_close()


class LiveRuleEngineTests(unittest.TestCase):
    """What a Live mark changes in the model's 5/10 tranche: the 9:45 state
    entry, cross entries only after 9:45, two entries a day, fixed stops, the
    forced 15:55 flat and no entry from the 15:55-16:00 bar."""
    D = date(2026, 9, 22)
    UNIT = ("abs", 0.5, "test unit")

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)
        self.store.save_settings({"bar_close_delay_min": 0})

    def tearDown(self):
        self.store.db.close()
        os.remove(self.path)

    def five(self, spec, day=None):
        """spec: [(close, high)] for consecutive 5-min bars from 9:30."""
        out, prev = [], spec[0][0]
        for k, (c, h) in enumerate(spec):
            t = datetime.combine(day or self.D, time(9, 30), tzinfo=ET) + k * FIVE
            out.append(Bar(t, t + FIVE, prev, max(h, prev, c), min(prev, c) - 0.05, c, 1000.0))
            prev = c
        return out

    def live_engine(self, bars=(), mode="short_only"):
        eng = Engine(self.store, ScriptedProvider(list(bars)), 5)
        eng.add_symbols("LIV", mode, 1.0, datetime.combine(self.D, time(9), tzinfo=ET))
        eng.set_live(1, ["EMA5_10"], datetime.combine(self.D, time(9), tzinfo=ET))
        return eng

    def sym(self):
        return self.store.q("SELECT * FROM symbols WHERE id=1")[0]

    def step(self, eng, bars, i, e5, e10):
        from engine import Fill
        b = bars[i]
        t = next(iter(self.store.q("SELECT * FROM tranches WHERE status='open'")), None)
        eng._live_510(self.sym(), t, Fill(b.close, b.end, b.session, "at the bar close"), bars,
                      bars, i, e5, e10, self.store.settings(), "ctx", lambda: self.UNIT)

    # ---- entries
    BARS = [(10.0, 10.3), (9.9, 10.1), (9.8, 10.0), (9.7, 9.9), (9.75, 9.9), (9.6, 9.8), (9.5, 9.7)]

    def test_state_entry_on_the_0945_bar_with_the_opening_range_high_stop(self):
        bars = self.five(self.BARS)
        eng = self.live_engine()
        below = ([9.0] * 7, [9.5] * 7)                # 5 under 10 all along: no cross anywhere
        for i in range(3):                             # bars ending 9:35, 9:40, 9:45
            self.step(eng, bars, i, *below)
        t = self.store.q("SELECT * FROM tranches")
        self.assertEqual(len(t), 1)
        t = t[0]
        self.assertEqual((t["side"], t["trigger"], _dt(t["entry_time"])), ("short", "state_0945", bars[2].end))
        self.assertEqual(t["stop_price"], 10.3)        # high of the 9:30-9:45 bars
        fill = 9.8 * (1 - 5 / 1e4)
        self.assertAlmostEqual(t["entry_price"], fill)
        self.assertAlmostEqual(t["stop_dist"], 10.3 - fill)
        self.assertAlmostEqual(t["risk_dollars"], t["qty"] * 0.5)  # still sized by the EMA unit

    def test_no_state_entry_when_the_5_is_above_or_equal_at_0945(self):
        bars = self.five(self.BARS)
        eng = self.live_engine()
        self.step(eng, bars, 2, [9.6] * 7, [9.5] * 7)
        self.step(eng, bars, 2, [9.5] * 7, [9.5] * 7)
        self.assertFalse(self.store.q("SELECT 1 FROM tranches"))

    def test_cross_before_0945_is_blocked_after_0945_enters_with_the_days_high(self):
        bars = self.five(self.BARS)
        eng = self.live_engine()
        e5, e10 = [9.6, 9.4, 9.6, 9.6, 9.6, 9.4, 9.4], [9.5] * 7   # down-crosses on 9:40 and 10:00
        self.step(eng, bars, 1, e5, e10)
        self.assertFalse(self.store.q("SELECT 1 FROM tranches"))
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE message LIKE '%before the 9:45 bar%'"))
        self.step(eng, bars, 2, e5, e10)                # 9:45: 5 above 10, no state entry
        self.step(eng, bars, 5, e5, e10)                # 10:00 down-cross
        t = self.store.q("SELECT * FROM tranches")[0]
        self.assertEqual((t["trigger"], _dt(t["entry_time"])), ("cross", bars[5].end))
        self.assertEqual(t["stop_price"], 10.3)         # day's high at entry (9:35 bar)

    def test_two_entries_a_day_at_most(self):
        bars = self.five(self.BARS)
        eng = self.live_engine()
        e5, e10 = [9.6, 9.6, 9.4, 9.6, 9.4, 9.6, 9.4], [9.5] * 7   # down at 9:45, 9:55, 10:05
        self.step(eng, bars, 2, e5, e10)                             # state entry
        self.store.x("UPDATE tranches SET status='closed', exit_time=?, exit_price=10.4, "
                     "exit_reason='stop: test', gross_pnl=-1", (bars[3].end.isoformat(),))
        self.step(eng, bars, 4, e5, e10)                             # second: a fresh cross
        self.store.x("UPDATE tranches SET status='closed', exit_time=?, exit_price=10.4, "
                     "exit_reason='stop: test', gross_pnl=-1 WHERE status='open'",
                     (bars[5].end.isoformat(),))
        self.step(eng, bars, 6, e5, e10)                             # third: refused
        rows = self.store.q("SELECT trigger FROM tranches ORDER BY id")
        self.assertEqual([r["trigger"] for r in rows], ["state_0945", "cross"])
        self.assertTrue(self.store.q("SELECT 1 FROM events WHERE message LIKE '%already 2 entries today%'"))

    def test_up_cross_covers_and_never_opens_a_live_long(self):
        bars = self.five(self.BARS)
        eng = self.live_engine()
        e5, e10 = [9.4, 9.4, 9.4, 9.6, 9.6, 9.6, 9.6], [9.5] * 7
        self.step(eng, bars, 2, e5, e10)
        self.step(eng, bars, 3, e5, e10)                 # up-cross at 9:50
        t = self.store.q("SELECT * FROM tranches")
        self.assertEqual(len(t), 1)
        self.assertTrue(t[0]["exit_reason"].startswith("5/10 EMA crossed up"))

    def test_non_live_symbol_keeps_cross_entries_before_0945_and_no_stop(self):
        bars = self.five(self.BARS)
        eng = Engine(self.store, ScriptedProvider([]), 5)
        eng.add_symbols("OFF", "short_only", 1.0, datetime.combine(self.D, time(9), tzinfo=ET))
        from engine import Fill
        b = bars[1]
        eng._ema_sleeve(self.sym(), "EMA5_10", None, Fill(b.close, b.end, b.session, "c"),
                        self.UNIT, self.store.settings(), True, False, "ctx")
        t = self.store.q("SELECT * FROM tranches")[0]
        self.assertEqual((t["trigger"], t["stop_dist"]), (None, None))

    def test_vwap_entries_record_their_stop_distance(self):
        from engine import Fill
        eng = Engine(self.store, ScriptedProvider([]), 5)
        eng.add_symbols("VW", "short_only", 1.0, datetime.combine(self.D, time(9), tzinfo=ET))
        b = self.five(self.BARS)[4]
        eng._enter(self.sym(), "VWAP", "short", Fill(10.0, b.end, b.session, "c"), None,
                   self.store.settings(), "test", stop_level=11.0)
        t = self.store.q("SELECT * FROM tranches")[0]
        self.assertAlmostEqual(t["stop_dist"], 11.0 - 10.0 * (1 - 5 / 1e4))

    def test_state_entry_end_to_end_through_tick(self):
        prev = self.D - timedelta(days=1)
        down = lambda p0, n: [(round(p0 - 0.01 * k, 4), round(p0 - 0.01 * k + 0.02, 4)) for k in range(n)]
        bars = self.five(down(12.0, 78), prev) + self.five(down(11.2, 12))
        eng = self.live_engine(bars)
        eng.tick(datetime.combine(self.D, time(10, 30), tzinfo=ET))
        t = self.store.q("SELECT * FROM tranches WHERE sleeve='EMA5_10'")  # (VWAP trades too, model only)
        self.assertEqual(len(t), 1)
        at945 = datetime.combine(self.D, time(9, 45), tzinfo=ET)
        self.assertEqual((t[0]["trigger"], _dt(t[0]["entry_time"])), ("state_0945", at945))
        self.assertEqual(t[0]["stop_price"], max(b.high for b in bars if b.session == self.D and b.end <= at945))

    # ---- the fixed stop
    def run_stop_day(self, spec, stop, trigger="cross", entry_k=6):
        bars = self.five(spec)
        eng = self.live_engine(bars)
        entry = bars[entry_k].end
        self.store.x("""INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
                        entry_price, stop_price, risk_dollars, fee_through, trigger, stop_dist)
                        VALUES (1,'LIV','EMA5_10','short',100,?,10,?,50,?,?,?)""",
                     (entry.isoformat(), stop, self.D.isoformat(), trigger, stop - 10))
        self.store.x("UPDATE symbols SET last_bar_end=? WHERE id=1", (entry.isoformat(),))
        eng.tick(bars[-1].end)
        return bars, self.store.q("SELECT * FROM tranches ORDER BY id")[0]

    def test_fixed_stop_does_not_rise_with_a_new_high(self):
        # stop 10.2. The 10:25 bar wicks to 10.4 but closes 10.15: no stop, and the stop
        # stays 10.2. The 10:35 close at 10.3 is below the new high but above 10.2: stop.
        spec = [(10.0, 10.1), (10.1, 10.2), (10.0, 10.1)] + [(9.9, 10.0)] * 8 + \
               [(10.15, 10.4), (10.1, 10.15), (10.3, 10.35), (10.0, 10.1)]
        bars, t = self.run_stop_day(spec, 10.2)
        self.assertEqual(t["status"], "closed")
        self.assertTrue(t["exit_reason"].startswith("stop: 5-min close 10.3 above the fixed stop 10.2"),
                        t["exit_reason"])
        self.assertEqual(_dt(t["exit_time"]), bars[13].end)
        self.assertAlmostEqual(t["exit_price"], 10.3 * (1 + 5 / 1e4))

    def test_close_equal_to_the_stop_is_not_a_stop(self):
        spec = [(10.0, 10.2)] + [(9.9, 10.0)] * 8 + [(10.2, 10.2), (10.0, 10.1)]
        _bars, t = self.run_stop_day(spec, 10.2)
        self.assertEqual(t["status"], "open")

    def test_no_fixed_stop_without_a_live_trigger(self):
        spec = [(10.0, 10.1), (10.1, 10.2)] + [(9.9, 10.0)] * 8 + [(10.5, 10.6), (10.4, 10.5)]
        _bars, t = self.run_stop_day(spec, 10.2, trigger=None)
        self.assertFalse((t["exit_reason"] or "").startswith("stop"))

    # ---- end of day
    def test_live_symbol_is_flat_at_1555_without_its_eod_switch(self):
        eng = Engine(self.store, ScriptedProvider([]), 5)
        eng.add_symbols("DAY", "short_only", 1.0, datetime.combine(self.D, time(9), tzinfo=ET))
        s = self.store.settings()
        self.assertIsNone(eng.day_only(self.sym(), "short", s))
        eng.set_live(1, ["EMA5_10"], datetime.combine(self.D, time(9), tzinfo=ET))
        self.assertEqual(self.sym()["eod_close"], 0)
        self.assertIn("close by end of day", eng.day_only(self.sym(), "short", s))

    def test_live_no_entry_from_1555_or_from_the_last_bar_at_the_next_open(self):
        from engine import Fill
        eng = self.live_engine()
        s = self.store.settings()
        at = lambda d, hh, mm: datetime.combine(d, time(hh, mm), tzinfo=ET)
        eng._enter(self.sym(), "EMA5_10", "short", Fill(10.0, at(self.D, 15, 55), self.D, "c"),
                   self.UNIT, s, "x", fixed_stop=10.5)
        nxt = self.D + timedelta(days=1)
        eng._enter(self.sym(), "EMA5_10", "short", Fill(10.0, at(nxt, 9, 30), nxt, "open"),
                   self.UNIT, s, "x", fixed_stop=10.5)
        self.assertFalse(self.store.q("SELECT 1 FROM tranches"))
        eng._enter(self.sym(), "EMA5_10", "short", Fill(10.0, at(self.D, 15, 50), self.D, "c"),
                   self.UNIT, s, "x", fixed_stop=10.5)
        self.assertEqual(len(self.store.q("SELECT 1 FROM tranches")), 1)  # 15:50 is fine


if __name__ == "__main__":
    unittest.main(verbosity=2)
