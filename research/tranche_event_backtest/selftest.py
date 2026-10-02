"""Self-test on SYNTHETIC bars only (the dashboard's own DemoProvider). No
market data, no network.

Checks that:
1. the replay's gross R per trade equals the engine's own records;
2. the workbook's net-R formula equals the engine's own net R when the engine
   itself runs with costs (exact for one-exit trades; scale-outs within 0.01R
   because the engine re-splits shares when equity changes);
3. after a LibreOffice recalculation, every workbook formula (trade net R,
   summary rows, entry-day rows) equals the same numbers computed in Python.

    python selftest.py          # needs LibreOffice (soffice) for check 3
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import date, datetime, time

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
import intraday
import replay
import report
import runner
import trades
from data import DemoProvider
from indicators import ET


def synthetic(tmp):
    dp = DemoProvider(date(2026, 1, 5))
    for sym in ("SYNA", "SYNB"):
        intraday.save_bars(os.path.join(tmp, f"{sym}.csv.gz"), dp._generate(sym, date(2026, 7, 31)))
    sessions = sorted({b.session for b in intraday.load_bars(os.path.join(tmp, "SYNA.csv.gz"))})

    def ev(sym, et, i, hf, sig=None):
        d0 = sessions[i]
        st = datetime.combine(d0, time(16), tzinfo=ET) if et == "CLOSE_100" else sig
        added = st if et == "CLOSE_100" else replay.signal_added_at(sig)
        return runner.EventRun(f"{et}:{sym}:{d0}", sym, et, d0, hf, st, added,
                               os.path.join(tmp, f"{sym}.csv.gz"), 0)
    events = [ev("SYNA", "CLOSE_100", 30, ""),
              ev("SYNA", "INTRADAY_100", 30, "HELD", datetime.combine(sessions[30], time(10, 15), tzinfo=ET)),
              ev("SYNA", "INTRADAY_100", 40, "FADED", datetime.combine(sessions[40], time(8, 5), tzinfo=ET)),
              ev("SYNB", "CLOSE_100", 100, "")]
    return events, sessions


def _legs(store, r):
    """The engine's closed tranche rows behind one extracted trade."""
    sleeve, trig = next(k for k, v in trades.STRATEGY.items() if v == r["strategy"])
    legs = store.q("SELECT * FROM tranches WHERE status='closed' AND entry_time=? AND sleeve=? AND side=?",
                   (r["entry_time"].isoformat(), sleeve, r["direction"]))
    return [t for t in legs if trig is None or t["trigger"] == trig]


def check_costs(events, sessions):
    e = events[0]
    bars = intraday.load_bars(e.bars_path)
    pause, end = runner.entry_window(e, sessions, sessions[-1])
    zero = replay.run_event(e.symbol, bars, "1h", e.added_at, sessions, pause, end)
    mark = bars[-1].close
    rows = [r for r in trades.extract(zero, sessions, mark) if r["status"] == "CLOSED"]
    for r in rows:
        legs = _legs(zero.store, r)
        assert abs(sum(t["gross_pnl"] for t in legs) / sum(t["risk_dollars"] for t in legs) - r["gross_r"]) < 1e-9
    saved = dict(replay.ZERO_COST)
    replay.ZERO_COST.update({"slippage_bps": 5.0, "borrow_rate_pct": 10.0})
    try:
        costed = replay.run_event(e.symbol, bars, "1h", e.added_at, sessions, pause, end)
    finally:
        replay.ZERO_COST.clear()
        replay.ZERO_COST.update(saved)
    worst = 0.0
    for r in rows:
        legs = _legs(costed.store, r)
        eng = sum(t["gross_pnl"] - t["borrow_fees"] for t in legs) / sum(t["risk_dollars"] for t in legs)
        diff = abs(eng - trades.net_r(r))
        assert diff < (1e-9 if len(r["legs"]) == 1 else 0.01), (r["strategy"], r["entry_time"], diff)
        worst = max(worst, diff)
    print(f"costs: {len(rows)} closed trades; workbook net R vs engine net R, worst |diff| {worst:.6f}")


def check_workbook(rows, tmp):
    if not shutil.which("soffice"):
        print("workbook: skipped (LibreOffice not installed)")
        return
    from openpyxl import load_workbook
    xl = os.path.join(tmp, "selftest.xlsx")
    report.write_workbook(xl, rows, [{"event": "synthetic"}], [{"excluded": "none"}], ["synthetic self-test"])
    subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx", "--outdir",
                    os.path.join(tmp, "calc"), xl], check=True, capture_output=True)
    wb = load_workbook(os.path.join(tmp, "calc", "selftest.xlsx"), data_only=True)
    ws = wb["Trades"]
    hdr = [c.value for c in ws[1]]
    py = {(r["event_id"], r["timeframe"], r["strategy"], r["direction"], r["entry_time"].replace(tzinfo=None)):
          trades.net_r(r) for r in rows}
    for v in ws.iter_rows(min_row=2, values_only=True):
        d = dict(zip(hdr, v))
        assert abs(d["Net R"] - py[(d["Event ID"], d["Timeframe"], d["Strategy"], d["Direction"], d["Entry time"])]) < 1e-9
    checked = 0
    for et in report.EVENT_TYPES:
        ws = wb[f"Summary {et}"]
        hdr = [c.value for c in ws[1]]
        for v in ws.iter_rows(min_row=2, values_only=True):
            d = dict(zip(hdr, v))
            sel = [r for r in rows if r["event_type"] == et and r["strategy"] == d["Strategy"]
                   and r["timeframe"] == d["Timeframe"] and r["direction"] == d["Direction"]
                   and r["status"] == "CLOSED"]
            assert d["Closed trades"] == len(sel)
            if not sel:
                continue
            nr = [trades.net_r(r) for r in sel]
            streak = cur = 0
            for r in sorted(sel, key=lambda r: r["entry_time"]):
                cur = cur + 1 if trades.net_r(r) <= 0 else 0
                streak = max(streak, cur)
            assert abs(d["Avg net R"] - statistics.mean(nr)) < 1e-9
            assert abs(d["Total net R"] - sum(nr)) < 1e-9
            assert abs(d["Win %"] - sum(x > 0 for x in nr) / len(nr)) < 1e-12
            assert abs(d["Avg gross R"] - statistics.mean(r["gross_r"] for r in sel)) < 1e-9
            assert abs(d["Worst trade (net R)"] - min(nr)) < 1e-9
            assert abs(d["Best trade (net R)"] - max(nr)) < 1e-9
            assert d["Largest losing streak"] == streak
            assert abs(d["Avg holding (trading days)"] - statistics.mean(r["holding_days"] for r in sel)) < 1e-9
            checked += 1
    ws = wb["By entry day"]
    total = sum(v[5] for v in ws.iter_rows(min_row=2, values_only=True))
    assert total == sum(r["status"] == "CLOSED" for r in rows)
    print(f"workbook: {len(rows)} trade formulas and {checked} summary rows match Python after recalculation")


def main():
    tmp = tempfile.mkdtemp(prefix="tranche_selftest_")
    events, sessions = synthetic(tmp)
    check_costs(events, sessions)
    rows = runner.run_all(events, sessions, sessions[-1], intraday.load_bars, books=("1h", "15m"))
    assert all(0 <= r["entry_day"] <= 25 for r in rows)
    for r in rows:
        if r["event_type"] == "CLOSE_100":
            assert r["entry_time"] >= datetime.combine(r["event_date"], time(16), tzinfo=ET)
        else:
            assert r["entry_time"] > max(r["signal_time"], datetime.combine(r["event_date"], time(9, 30), tzinfo=ET))
    print(f"runner: {len(rows)} trades, {sum(bool(r['also_under']) for r in rows)} also under another event")
    check_workbook(rows, tmp)
    shutil.rmtree(tmp)
    print("SELFTEST PASS")


if __name__ == "__main__":
    sys.exit(main())
