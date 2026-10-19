"""Hand-check helper: everything needed to verify one trade from the raw bars.

    python explain_trade.py EVENT_ID TIMEFRAME ENTRY_TIME
    e.g. python explain_trade.py INTRADAY_100:ABCD:2026-03-02 15m 2026-03-02T10:45:00-05:00

Prints (1) the engine's own bar table (tranche-dashboard/engine.py:348-379:
bars, EMA5/10/20, ATR, VWAP and which signals fired) for the bars leading up to
the entry and to the exit, (2) the engine's entry/exit log lines for the trade,
and (3) the raw 5-minute bars of the entry and exit bars. The working in
out/hand_checks.md is written from this output and checked against the bars.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
import intraday
import replay
import runner
from run_backtest import CACHE, OUT, STATE, all_sessions


def main(eid: str, book: str, entry: str) -> None:
    et, sym, d0 = eid.split(":")
    sessions, data_end = all_sessions()
    bars_path = os.path.join(CACHE, "event_bars", f"{eid.replace(':', '_')}.csv.gz")
    bars = intraday.load_bars(bars_path)
    extra = json.load(open(STATE)).get(eid, 0) if os.path.exists(STATE) else 0
    trades_csv = os.path.join(OUT, "trades.csv")
    signal = None
    for r in csv.DictReader(open(trades_csv)):
        if r["event_id"] == eid:
            signal = datetime.fromisoformat(r["signal_time"])
            break
    if signal is None:
        raise SystemExit("event not in out/trades.csv")
    day0 = date.fromisoformat(d0)
    added = replay.signal_added_at(signal) if et == "INTRADAY_100" else replay.close_added_at(day0)
    e = runner.EventRun(eid, sym, et, day0, "", signal, added, bars_path, 0)
    pause, end = runner.entry_window(e, sessions, data_end, extra)
    run = replay.run_event(sym, bars, book, added, sessions, pause, end)
    t = run.store.q("SELECT * FROM tranches WHERE entry_time=?", (entry,))
    if not t:
        raise SystemExit("no trade with that entry time in this book")
    delay = timedelta(minutes=run.store.settings()["bar_close_delay_min"])
    print(f"== {eid} {book} entry {entry}")
    for row in t:
        print({k: row[k] for k in ("sleeve", "trigger", "side", "qty", "entry_price", "risk_dollars",
                                   "exit_time", "exit_price", "exit_reason", "gross_pnl")})
    for ev in run.store.q("SELECT ts, sleeve, kind, message FROM events WHERE kind IN ('entry','exit') "
                          "AND (ts=? OR ts IN (SELECT exit_time FROM tranches WHERE entry_time=?)) ORDER BY ts",
                          (entry, entry)):
        print(f"[{ev['ts']}] {ev['sleeve']} {ev['kind']}: {ev['message']}")
    for label, when in (("entry", entry), ("exit", t[-1]["exit_time"])):
        if not when:
            continue
        now = datetime.fromisoformat(when) + delay
        print(f"\n-- engine bar table up to the {label} ({now:%Y-%m-%d %H:%M})")
        for r in run.engine.bar_table(sym, now, n=8):
            print(r)
        w = datetime.fromisoformat(when)
        print(f"-- raw 5-minute bars around the {label}")
        for b in bars:
            if w - timedelta(minutes=run.engine.minutes + 5) <= b.start <= w + timedelta(minutes=5):
                print(b.start.strftime("%Y-%m-%d %H:%M"), b.open, b.high, b.low, b.close, int(b.volume))


if __name__ == "__main__":
    main(*sys.argv[1:4])
