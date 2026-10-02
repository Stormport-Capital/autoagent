"""Offline dry run of the whole PC pipeline with a FAKE FMP (no network, no
key, no allowance used): security_master -> events -> probe -> plan_pull ->
fetch_bars -> run_backtest, on a copy of this folder with 3 real candidate
events and synthetic 5-minute bars (pre-market and after-hours included,
day 0 opening at about 2.1x the prior close).

    python pipeline_test.py          # ~5-10 minutes; leaves nothing behind

Proves the scripts run end to end and write the workbook, the HTML and the
trade list. It says nothing about real FMP data.
"""

from __future__ import annotations

import csv
import glob
import importlib
import math
import os
import random
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.normpath(os.path.join(HERE, "..", "..", "tranche-dashboard"))


class FakeResponse:
    def __init__(self, body):
        import json
        self.status_code, self._body = 200, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeFmp:
    """Answers /stable/profile and /stable/historical-chart/5min like FMP."""

    def __init__(self, events: dict[str, tuple[date, date, float]], splits, sessions):
        self.events = events   # symbol -> (prior day, day 0, prior as-traded close)
        self.splits = splits   # events.load_splits(): bars come back split-ADJUSTED, like a smooth series
        self.sessions = set(sessions)

    def get(self, url, params=None, timeout=None):
        sym = params["symbol"]
        if url.endswith("/profile"):
            return FakeResponse([{"symbol": sym, "exchange": "NASDAQ", "exchangeFullName": "NASDAQ Capital Market",
                                  "isEtf": False, "isFund": False, "isAdr": False, "isActivelyTrading": True,
                                  "ipoDate": "2015-01-02", "companyName": f"{sym} Test Holdings"}])
        ref_day, d0, ref = self.events.get(sym, (date(2000, 1, 3), date(2100, 1, 4), 500.0))  # e.g. SPY
        k = 1.0
        for sd, num, den in self.splits.get(sym, []):
            if sd > ref_day:
                k *= float(den / num)
        ref *= k   # the prior close on the adjusted basis
        a, b = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        rows, d = [], a
        while d <= b:
            if d.weekday() < 5 and (d > max(self.sessions) or d in self.sessions):
                rng = random.Random(f"{sym}{d}")
                level = ref if d < d0 else ref * 2.1 * math.exp(-0.02 * (d - d0).days)
                t = datetime.combine(d, time(4, 0))
                while t.time() < time(20, 0):
                    p = level * (1 + rng.gauss(0, 0.01))
                    if d == d0 and t.time() < time(9, 30):
                        p = ref * (1 + rng.gauss(0, 0.005))
                    o, c = p, p * (1 + rng.gauss(0, 0.004))
                    rows.append({"date": t.strftime("%Y-%m-%d %H:%M:%S"), "open": round(o, 4),
                                 "high": round(max(o, c) * 1.003, 4), "low": round(min(o, c) * 0.997, 4),
                                 "close": round(c, 4), "volume": rng.randint(1000, 90000)})
                    t += timedelta(minutes=5)
            d += timedelta(days=1)
        return FakeResponse(sorted(rows, key=lambda r: r["date"], reverse=True))


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="tranche_pipeline_")
    work = os.path.join(tmp, "research", "tranche_event_backtest")
    shutil.copytree(HERE, work, ignore=shutil.ignore_patterns("cache", "out", "__pycache__"))
    os.symlink(DASH, os.path.join(tmp, "tranche-dashboard"))
    # keep three real candidate rows that pass price and volume: two days of one list, one of the other
    sys.path.insert(0, HERE)
    import events as ev0
    splits = ev0.load_splits()

    def split_in_window(r):
        d0 = date.fromisoformat(r["event_date"])
        return any(d0 - timedelta(days=35) < sd <= d0 + timedelta(days=100) and den / num >= 2
                   for sd, num, den in splits.get(r["symbol"], []))
    picked, keep = [], {}
    for name in ("INTRADAY_100", "CLOSE_100"):
        rows = list(csv.DictReader(open(os.path.join(HERE, "out", f"events_{name}.csv"))))
        rows = [r for r in rows if not r["same_bars_as"] and r["prior_sessions"] == "20"]
        if name == "INTRADAY_100":   # one with a split inside its window, so the probe can read the basis
            picked += [next(r for r in rows if split_in_window(r))] + rows[:1]
        else:
            picked += [r for r in rows if r["symbol"] not in {p["symbol"] for p in picked}][:1]
    sys.path.remove(HERE)
    for m in ("events", "spec"):
        sys.modules.pop(m, None)
    for r in picked:
        keep[(r["symbol"], r["event_date"])] = r
    for f in glob.glob(os.path.join(work, "inputs", "cand_*.csv")):
        lines = [ln for ln in open(f) if (ln.split(",")[0].strip(), ln.split(",")[1]) in keep]
        open(f, "w").writelines(lines)
    sys.path.insert(0, work)
    os.chdir(work)
    os.environ["FMP_API_KEY"] = "offline-test-not-a-key"
    fake = FakeFmp({r["symbol"]: (date.fromisoformat(r["ref_date"]), date.fromisoformat(r["event_date"]),
                                  float(r["ref_close_raw"])) for r in picked}, splits, ev0.load_sessions())
    for m in ("spec", "events", "fmp", "security_master", "plan_pull", "probe", "intraday", "fetch_bars",
              "replay", "trades", "runner", "report", "html_summary", "run_backtest"):
        sys.modules.pop(m, None)
    fmp = importlib.import_module("fmp")
    orig = fmp.Fmp.__init__

    def init(self, max_calls, session=None):
        orig(self, max_calls, session=fake)
    fmp.Fmp.__init__ = init
    events = importlib.import_module("events")
    steps = [("security_master", ["--max-calls", "10"]), ("events", []), ("probe", []),
             ("plan_pull", []), ("fetch_bars", ["--max-calls", "200"]), ("run_backtest", ["--processes", "4", "--check-formulas"])]
    out = os.path.join(work, "out")
    for _round in range(6):   # extensions for trades still open, as on the PC
        for step, argv in steps:
            print(f"\n### {step} {' '.join(argv)}")
            sys.argv = [step] + argv
            importlib.import_module(step).main()
        if os.path.exists(os.path.join(out, "tranche_event_backtest.xlsx")):
            break
        steps = [("fetch_bars", ["--max-calls", "200", "--extend"]),
                 ("run_backtest", ["--processes", "4", "--check-formulas"])]
    import json
    print("probe basis reading:", json.load(open(os.path.join(out, "probe.json")))["call1_split"]["reading"])
    for f in ("tranche_event_backtest.xlsx", "summary.html", "trades.csv", "hand_check_picks.csv"):
        assert os.path.exists(os.path.join(out, f)), f
    n = sum(1 for _ in open(os.path.join(out, "trades.csv"))) - 1
    print(f"\nPIPELINE TEST PASS: {len(picked)} events, {n} trades; outputs in {out} (deleted now)")
    os.chdir(HERE)
    shutil.rmtree(tmp)
    del events


if __name__ == "__main__":
    main()
