"""Offline tests for gap_intraday_test.py: python -I test_gap_intraday.py"""
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gap_intraday_test as g  # noqa: E402


def bar(t, o, h, lo, c, v=1000, day="2026-03-02"):
    return {"date": f"{day} {t}:00", "open": o, "high": h, "low": lo, "close": c, "volume": v}


def flat_day(px=100.0, day="2026-03-02"):
    """Premarket 4:00-9:30 and regular 9:30-16:00 bars, all at px."""
    out = []
    for hh in range(4, 16):
        for mm in range(0, 60, 5):
            t = f"{hh:02d}:{mm:02d}"
            if "04:00" <= t < "16:00":
                out.append(bar(t, px, px, px, px, 2000 if t < "09:30" else 1000, day))
    return out


def with_bars(bars, **edits):
    out = []
    for b in bars:
        t = b["date"][11:16]
        if t in edits:
            o, h, lo, c = edits[t]
            b = {**b, "open": o, "high": h, "low": lo, "close": c}
        out.append(b)
    return out


class SimulateTests(unittest.TestCase):
    def reg(self, **edits):
        return g.split_session(with_bars(flat_day(), **edits))[1]

    def test_entry_at_the_stop_when_the_bar_opens_above_it(self):
        r = g.simulate(self.reg(**{"09:40": (100, 100, 97.5, 97.5), "15:50": (95, 95, 95, 95)}), 100, slip_bps=0)
        self.assertEqual((r["entry_time"], r["entry_px"]), ("09:40", 98.0))
        self.assertEqual((r["exit_reason"], r["exit_px"]), ("flat at 15:55", 95.0))
        self.assertAlmostEqual(r["ret_pct"], (98 - 95) / 98 * 100, places=3)

    def test_entry_at_the_bar_open_between_limit_and_stop(self):
        r = g.simulate(self.reg(**{"09:35": (97.8, 98, 97, 97.2)}), 100, slip_bps=0)
        self.assertEqual((r["entry_time"], r["entry_px"]), ("09:35", 97.8))

    def test_no_fill_when_it_opens_below_the_limit_and_stays_there(self):
        r = g.simulate(self.reg(**{"09:35": (96, 97.2, 95, 95.5)}), 100, slip_bps=0)
        self.assertIsNone(r["entry_time"])

    def test_fill_at_the_limit_when_it_trades_back_up_through_it(self):
        r = g.simulate(self.reg(**{"09:35": (96, 98.2, 95, 98)}), 100, slip_bps=0)
        self.assertEqual(r["entry_px"], 97.5)

    def test_no_entry_after_the_deadline_or_before_935(self):
        self.assertIsNone(g.simulate(self.reg(**{"09:30": (100, 100, 96, 96)}), 100)["entry_time"])
        self.assertIsNone(g.simulate(self.reg(**{"11:30": (100, 100, 96, 96)}), 100)["entry_time"])
        self.assertEqual(g.simulate(self.reg(**{"11:25": (100, 100, 96, 96)}), 100)["entry_time"], "11:25")

    def test_stop_touched_later_covers_at_the_level_or_the_gap_open(self):
        r = g.simulate(self.reg(**{"09:40": (100, 100, 97, 97), "10:00": (100, 104.5, 99, 104)}), 100, slip_bps=0)
        self.assertEqual((r["exit_time"], r["exit_reason"], r["exit_px"]), ("10:00", "stop touched", 103.88))
        r = g.simulate(self.reg(**{"09:40": (100, 100, 97, 97), "10:00": (105, 106, 104, 105)}), 100, slip_bps=0)
        self.assertEqual(r["exit_px"], 105.0)  # gapped through: filled at the bar's open

    def test_entry_bar_touching_the_stop_counts_as_stopped(self):
        r = g.simulate(self.reg(**{"09:40": (100, 104, 97, 97)}), 100, slip_bps=0)
        self.assertEqual(r["exit_reason"], "stop touched (entry bar)")
        self.assertAlmostEqual(r["ret_pct_no_stop"], (98 - 100) / 98 * 100, places=2)  # flat at 100, short from 98

    def test_close_stop_needs_a_close_above_it(self):
        r = g.simulate(self.reg(**{"09:40": (100, 100, 97, 97), "10:00": (100, 104.5, 99, 100),
                                   "15:50": (95, 95, 95, 95)}), 100, slip_bps=0)
        self.assertEqual(r["exit_reason"], "stop touched")        # intrabar: out at 103.88
        self.assertGreater(r["ret_pct_close_stop"], 0)             # close rule: never closed above, flat at 95
        self.assertAlmostEqual(r["ret_pct_close_stop"], (98 - 95) / 98 * 100, places=3)

    def test_slippage_goes_against_the_short_both_ways(self):
        r = g.simulate(self.reg(**{"09:40": (100, 100, 97, 97), "15:50": (95, 95, 95, 95)}), 100, slip_bps=100)
        self.assertAlmostEqual(r["entry_px"], 98 * 0.99, places=4)
        self.assertAlmostEqual(r["exit_px"], 95 * 1.01, places=4)


class VolumeTests(unittest.TestCase):
    def test_pace_and_adv(self):
        pre, reg = g.split_session(flat_day())
        self.assertEqual((len(pre), len(reg)), (66, 78))
        p = g.pace_935(pre, reg, adv=100_000)
        self.assertEqual((p["vol_pm"], p["vol_930"], p["vol_935"]), (132_000, 1000, 133_000))
        self.assertAlmostEqual(p["pace"], 133_000 / 8000)
        eod = [{"date": f"2026-01-{d:02d}", "volume": 10} for d in range(1, 22)] + [{"date": "2026-01-22", "volume": 99}]
        self.assertEqual(g.adv20(eod, "2026-01-22"), 10)
        self.assertIsNone(g.adv20(eod[:15], "2026-01-22"))


class EndToEndTests(unittest.TestCase):
    """The whole run from cached FMP answers: no network."""

    def test_run_from_cache(self):
        d = Path(tempfile.mkdtemp())
        day = "2026-03-02"
        cache = d / "out" / "cache"
        cache.mkdir(parents=True)
        bars = with_bars(flat_day(100.0, day), **{"09:40": (100, 100, 97, 97), "15:50": (94, 94, 94, 94)})
        (cache / f"5min_AAA_{day}.json").write_text(json.dumps(bars))
        eod = [{"date": f"2026-02-{k:02d}", "open": 120, "close": 120, "volume": 500_000} for k in range(2, 28)]
        eod += [{"date": day, "open": 100, "close": 94, "volume": 5_000_000}]
        (cache / "eod_AAA.json").write_text(json.dumps(eod))
        # an ETF: 3x name, gaps -12% on the same day, same bars
        (cache / "etf_list.json").write_text(json.dumps([{"symbol": "LEV", "name": "Direxion Daily Thing Bull 3X"},
                                                         {"symbol": "PLN", "name": "Plain Index ETF"}]))
        (cache / "eod_LEV.json").write_text(json.dumps([{**r, "open": 100 if r["date"] == day else 114,
                                                         "close": 94 if r["date"] == day else 114,
                                                         "volume": 3_000_000} for r in eod]))
        (cache / "eod_PLN.json").write_text(json.dumps([{**r, "volume": 10} for r in eod]))  # too thin
        (cache / f"5min_LEV_{day}.json").write_text(json.dumps(bars))
        csvp = d / "ev.csv"
        with open(csvp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "trade_date", "prev_close", "open", "low", "high", "close", "gap_open_pct",
                        "close_vs_prev_pct", "close_vs_open_pct", "low_vs_open_pct", "high_vs_open_pct",
                        "vol_multiple", "flag"])
            w.writerow(["AAA", day, 120, 100, 94, 100, 94, -16.7, -21.7, -6, -6, 0, 10.0, ""])
            w.writerow(["BAD", day, 1.5, 1.2, 1.1, 1.3, 1.2, -20, -20, 0, -8, 8, 3, ""])   # below $15: out
            w.writerow(["FLG", day, 120, 100, 94, 100, 94, -16.7, -21.7, -6, -6, 0, 10.0, "REVIEW"])
        os.environ["FMP_API_KEY"] = "testkey"
        try:
            g.main(["--csv", str(csvp), "--out", str(d / "out"), "--etfs", "--slip-bps", "0",
                    "--start", "2026-02-01", "--end", "2026-03-05"])
        finally:
            os.environ.pop("FMP_API_KEY")
        rows = list(csv.DictReader(open(d / "out" / "trades.csv")))
        self.assertEqual([(r["symbol"], r["kind"]) for r in rows], [("AAA", "stock"), ("LEV", "lev_etf")])
        a = rows[0]
        self.assertEqual((a["entry_time"], a["entry_px"], a["exit_reason"]), ("09:40", "98.0", "flat at 15:55"))
        self.assertAlmostEqual(float(a["ret_pct"]), (98 - 94) / 98 * 100, places=2)
        self.assertEqual(a["adv20"], "500000")
        self.assertAlmostEqual(float(a["pace"]), 133_000 / (0.08 * 500_000), places=2)
        self.assertEqual(a["eod_mult"], "10.0")
        text = (d / "out" / "summary.txt").read_text()
        self.assertIn("=== stock ===", text)
        self.assertIn("=== lev_etf ===", text)
        self.assertIn("2026, intrabar stop", text)
        etfs = list(csv.DictReader(open(d / "out" / "etf_events.csv")))
        self.assertEqual([e["symbol"] for e in etfs], ["LEV"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
