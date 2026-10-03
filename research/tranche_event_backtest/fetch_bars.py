"""Step E.1: pull the approved 5-minute bars into local files (no database).

    python fetch_bars.py --max-calls N            # every TESTED event window
    python fetch_bars.py --max-calls N --extend   # more sessions for trades still open

One request per CHUNK_DAYS calendar days per symbol window (plan_pull.py),
saved as cache/raw/<SYMBOL>/<from>_<to>.json; a chunk already on disk costs no
call, so an interrupted pull resumes where it stopped. Bars are then merged
into cache/bars/<SYMBOL>.csv.gz exactly as FMP sent them (as-traded or
adjusted, whatever probe.py found). --extend reads out/extend.json written by
run_backtest.py and pulls EXIT_EXTRA_SESSIONS more sessions for each listed
symbol.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta

import events
import intraday
import plan_pull
import spec
from fmp import Fmp, BudgetExceeded
from indicators import ET, Bar

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "cache", "raw")
BARS = os.path.join(HERE, "cache", "bars")
EXTEND = os.path.join(HERE, "out", "extend.json")


def chunks(a: date, b: date):
    d = a
    while d <= b:
        e = min(d + timedelta(days=plan_pull.CHUNK_DAYS - 1), b)
        yield d, e
        d = e + timedelta(days=1)


def tested_windows() -> dict[str, list[tuple[date, date]]]:
    rows, splits, sessions = events.load_candidates(), events.load_splits(), events.load_sessions()
    master = events.load_master()
    tested = []
    for name in spec.ACTIVE:
        tested += [e for e in events.build(spec.SPECS[name], rows, splits, sessions, master)
                   if e["status"] == "TESTED"]
    return plan_pull.windows(tested, sorted(set(sessions) | set(plan_pull.EXTRA_SESSIONS)))


def merge(sym: str) -> int:
    seen: dict[datetime, Bar] = {}
    folder = os.path.join(RAW, sym)
    for fn in sorted(os.listdir(folder)):
        with open(os.path.join(folder, fn)) as f:
            for r in json.load(f) or []:
                try:
                    start = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                    seen[start] = Bar(start, start + intraday.FIVE, float(r["open"]), float(r["high"]),
                                      float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
                except (KeyError, TypeError, ValueError):
                    continue
    intraday.save_bars(os.path.join(BARS, f"{sym}.csv.gz"), list(seen.values()))
    return len(seen)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-calls", type=int, required=True)
    ap.add_argument("--extend", action="store_true")
    args = ap.parse_args()
    if args.extend:
        with open(EXTEND) as f:
            want = {s: [(date.fromisoformat(a), date.fromisoformat(b))] for s, (a, b) in json.load(f).items()}
    else:
        want = tested_windows()
    todo = []
    for sym, ws in sorted(want.items()):
        for a, b in ws:
            for x, y in chunks(a, b):
                path = os.path.join(RAW, sym, f"{x}_{y}.json")
                if not os.path.exists(path):
                    todo.append((sym, x, y, path))
    print(f"{len(want)} symbols, {len(todo)} chunks still to fetch (budget {args.max_calls})")
    if len(todo) > args.max_calls:
        raise SystemExit(f"STOP: {len(todo)} calls needed, budget {args.max_calls}. Ask Dean.")
    fmp = Fmp(args.max_calls)
    try:
        for sym, x, y, path in todo:
            body, _n = fmp.chart_5min(sym, x, y)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".partial", "w") as f:
                json.dump(body, f)
            os.replace(path + ".partial", path)
    except BudgetExceeded as e:
        print(f"stopped: {e}")
    finally:
        print(f"FMP calls this run: {fmp.calls}, {fmp.bytes / 1e6:.1f} MB")
    total = sum(merge(sym) for sym in sorted(want) if os.path.isdir(os.path.join(RAW, sym)))
    print(f"merged {total:,} bars into {BARS}")


if __name__ == "__main__":
    main()
