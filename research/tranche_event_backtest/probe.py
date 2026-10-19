"""Step D: at most 3 single-symbol FMP calls to learn what the 5-minute data
looks like before sizing the pull (Dean's PC prompt, Step D).

    python probe.py

Call 1, a symbol with a recorded split inside its event window, around the
split date: are bars split-adjusted or as-traded (a step of about the split
ratio across the split date means as-traded), are pre-market bars included,
is the time stamp the bar start (first regular bar 09:30) or end (09:35), how
many bars and bytes come back for a 5-session request.
Call 2, the event symbol with the earliest window start that FMP no longer
lists as actively trading, at that start: does history reach back that far
for a delisted name.
Call 3, SPY at the earliest window start: does 5-minute history reach the
oldest date the run needs.
Writes out/probe.json. Needs inputs/security_master.csv (Step C) for call 2.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import events
import plan_pull
import spec
from fmp import Fmp

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "probe.json")


def bars_summary(body) -> dict:
    rows = body if isinstance(body, list) else []
    stamps = sorted(r["date"] for r in rows if "date" in r)
    times = sorted({s[11:16] for s in stamps})
    return {"bars": len(rows), "first": stamps[0] if stamps else None, "last": stamps[-1] if stamps else None,
            "earliest_time_of_day": times[0] if times else None, "latest_time_of_day": times[-1] if times else None,
            "premarket_bars": sum(1 for s in stamps if s[11:16] < "09:30"),
            "afterhours_bars": sum(1 for s in stamps if s[11:16] >= "16:00")}


def main() -> None:
    rows, splits, sessions = events.load_candidates(), events.load_splits(), events.load_sessions()
    master = events.load_master()
    tested = []
    for name in spec.ACTIVE:
        tested += [e for e in events.build(spec.SPECS[name], rows, splits, sessions, master)
                   if e["status"] == "TESTED"]
    merged = plan_pull.windows(tested, sorted(set(sessions) | set(plan_pull.EXTRA_SESSIONS)))
    fmp = Fmp(max_calls=3)
    out = {}

    # call 1: split inside a window
    pick = None
    for sym, ws in sorted(merged.items()):
        for a, b in ws:
            for d, num, den in splits.get(sym, []):
                if a < d <= b and den / num >= 2:
                    pick = (sym, d, float(den / num))
                    break
            if pick:
                break
        if pick:
            break
    if pick:
        sym, d, factor = pick
        body, n = fmp.chart_5min(sym, d - timedelta(days=4), d + timedelta(days=1))
        s = bars_summary(body)
        rows_ = sorted(body, key=lambda r: r["date"]) if isinstance(body, list) else []
        before = [r for r in rows_ if r["date"][:10] < d.isoformat() and "09:30" <= r["date"][11:16] < "16:00"]
        after = [r for r in rows_ if r["date"][:10] >= d.isoformat() and "09:30" <= r["date"][11:16] < "16:00"]
        step = (float(after[0]["open"]) / float(before[-1]["close"])) if before and after else None
        out["call1_split"] = {"symbol": sym, "split_date": d.isoformat(), "split_factor": factor,
                              "bytes": n, **s, "last_close_before": before[-1] if before else None,
                              "first_open_after": after[0] if after else None, "step_across_split": step,
                              "reading": "UNKNOWN" if step is None else
                              ("as-traded (step about the split factor)" if abs(step / factor - 1) < 0.35
                               else "split-adjusted (no step)" if abs(step - 1) < 0.35 else "UNCLEAR")}
    else:
        out["call1_split"] = "no event window contains a split of 2x or more"

    # call 2: a delisted symbol at its window start
    start = min(a for ws in merged.values() for a, _b in ws)
    gone = [(ws[0][0], sym) for sym, ws in merged.items()
            if master and str(master.get(sym, {}).get("is_actively_trading")).lower() == "false"]
    if gone:
        a, sym = min(gone)
        body, n = fmp.chart_5min(sym, a, a + timedelta(days=2))
        out["call2_delisted"] = {"symbol": sym, "requested_from": a.isoformat(), "bytes": n, **bars_summary(body)}
    else:
        out["call2_delisted"] = "no tested symbol is marked not actively trading"

    # call 3: SPY at the earliest date the run needs
    body, n = fmp.chart_5min("SPY", start, start + timedelta(days=2))
    out["call3_depth"] = {"symbol": "SPY", "requested_from": start.isoformat(), "bytes": n, **bars_summary(body)}
    out["fmp_calls"] = fmp.calls
    out["run_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
