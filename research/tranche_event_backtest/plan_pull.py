"""Step 3 sizing: how many symbols, bars and FMP requests the 5-minute pull needs.

Per event the pull covers the engine's 35-calendar-day look-back before day 0
(spec.ENGINE_WINDOW_DAYS, the live window in tranche-dashboard/data.py:212,227)
through trading day 25 plus 60 more sessions for exits (Dean, 2026-10-02),
capped at the last complete session. Overlapping windows of one symbol are
merged so no day is requested twice.

Requests assume one call per 7 calendar days, the chunk size the dashboard's
own FMP client uses (tranche-dashboard/data.py:213-217); FMP's real per-call
limit is measured by the first probe. Bytes per bar is an estimate until the
probe measures it.

    python plan_pull.py [statuses]   # default: PENDING TESTED
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date, timedelta

import events
import spec

LAST_SESSION = date(2026, 10, 1)       # last complete session when this was sized
EXTRA_SESSIONS = [date(2026, 10, 1), date(2026, 10, 2)]  # not yet in gold
RTH_BARS = 78                          # 9:30-16:00 in 5-minute bars
EXT_BARS = 192                         # 4:00-20:00 if FMP includes extended hours
CHUNK_DAYS = 7
BYTES_PER_BAR = 110                    # estimate; measured by the first probe


def windows(evts: list[dict], sessions: list[date]) -> dict[str, list[tuple[date, date]]]:
    idx = {d: i for i, d in enumerate(sessions)}
    per = defaultdict(list)
    for e in evts:
        d0 = date.fromisoformat(e["event_date"])
        i = idx[d0] + spec.ENTRY_LAST_DAY + spec.EXIT_EXTRA_SESSIONS
        end = sessions[i] if i < len(sessions) else LAST_SESSION
        per[e["symbol"]].append((d0 - timedelta(days=spec.ENGINE_WINDOW_DAYS), min(end, LAST_SESSION)))
    merged = {}
    for sym, ws in per.items():
        ws.sort()
        out = [list(ws[0])]
        for a, b in ws[1:]:
            if a <= out[-1][1] + timedelta(days=1):
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        merged[sym] = [tuple(x) for x in out]
    return merged


def size(merged, sessions) -> dict:
    ss = sorted(set(sessions) | set(EXTRA_SESSIONS))
    n_sessions = requests = 0
    for ws in merged.values():
        for a, b in ws:
            n_sessions += sum(1 for d in ss if a <= d <= b)
            requests += math.ceil(((b - a).days + 1) / CHUNK_DAYS)
    return {"symbols": len(merged), "windows": sum(len(w) for w in merged.values()),
            "sessions": n_sessions, "requests": requests,
            "bars_rth": n_sessions * RTH_BARS, "bars_ext": n_sessions * EXT_BARS,
            "mb_rth": n_sessions * RTH_BARS * BYTES_PER_BAR / 1e6,
            "mb_ext": n_sessions * EXT_BARS * BYTES_PER_BAR / 1e6}


def splits_inside(merged, splits) -> int:
    return sum(1 for sym, ws in merged.items() for a, b in ws
               for s in splits.get(sym, []) if a <= s[0] <= b)


def main(statuses=("PENDING", "TESTED")) -> None:
    rows, splits = events.load_candidates(), events.load_splits()
    sessions = events.load_sessions()
    full = sorted(set(sessions) | set(EXTRA_SESSIONS))
    # trading days after the last gold date are needed for day-25+60 windows of
    # late events; they are capped at LAST_SESSION anyway
    picked = []
    for name in spec.ACTIVE:
        evs = events.build(spec.SPECS[name], rows, splits, sessions, events.load_master())
        picked += [e for e in evs if e["status"] in statuses]
    merged = windows(picked, full)
    s = size(merged, full)
    s["events"] = len(picked)
    s["windows_with_split_inside"] = splits_inside(merged, splits)
    for k, v in s.items():
        print(f"{k:28} {v:,.1f}" if isinstance(v, float) else f"{k:28} {v:,}")


if __name__ == "__main__":
    main(tuple(sys.argv[1:]) or ("PENDING", "TESTED"))
