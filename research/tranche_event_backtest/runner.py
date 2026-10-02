"""Step 4 driver: every tested event x bar size through the engine replay.

Each (event, bar size) is its own fresh book (Dean, answer 11), so two events
of one symbol inside 25 days both count; trades that show up under more than
one event are flagged afterwards (`also_under`).

    from runner import EventRun, run_all
"""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
import replay
import spec
import trades
from indicators import in_rth

BOOK_ORDER = ("1h", "15m", "5m")


@dataclass(frozen=True)
class EventRun:
    event_id: str            # e.g. CLOSE_100:ABCD:2026-03-02
    symbol: str
    event_type: str
    event_date: date
    held_faded: str          # INTRADAY_100 only
    signal_time: datetime    # end of the signal bar (INTRADAY) or day 0 16:00 (CLOSE)
    added_at: datetime       # engine gate: fills must come after this
    bars_path: str           # cached 5-minute bars, one basis for the whole window
    splits_applied: int      # splits rescaled inside the window (Dean, answer 10)


def entry_window(e: EventRun, sessions: list[date], data_end: date) -> tuple[date | None, date]:
    """(first session with no new entries, last session to simulate)."""
    i = sessions.index(e.event_date)
    pause = sessions[i + spec.ENTRY_LAST_DAY + 1] if i + spec.ENTRY_LAST_DAY + 1 < len(sessions) else None
    j = i + spec.ENTRY_LAST_DAY + spec.EXIT_EXTRA_SESSIONS
    end = sessions[j] if j < len(sessions) else data_end
    return pause, min(end, data_end)


def _one(args) -> list[dict]:
    e, book, sessions, data_end, load = args
    bars = load(e.bars_path)
    pause, end = entry_window(e, sessions, data_end)
    run = replay.run_event(e.symbol, bars, book, e.added_at, sessions, pause, end)
    i0 = sessions.index(e.event_date)
    rows = []
    mark = next(b.close for b in reversed(bars) if b.session <= end and in_rth(b.start))
    for t in trades.extract(run, sessions, mark):
        t.update({
            "event_id": e.event_id, "symbol": e.symbol, "event_type": e.event_type,
            "event_date": e.event_date, "held_faded": e.held_faded,
            "signal_time": e.signal_time, "splits_applied": e.splits_applied,
            "entry_day": sessions.index(t["entry_time"].date()) - i0,
        })
        rows.append(t)
    run.store.db.close()
    return rows


def run_all(events: list[EventRun], sessions: list[date], data_end: date, load,
            books=BOOK_ORDER, processes: int | None = None) -> list[dict]:
    jobs = [(e, b, sessions, data_end, load) for e in events for b in books]
    rows: list[dict] = []
    with ProcessPoolExecutor(max_workers=processes or os.cpu_count()) as pool:
        for part in pool.map(_one, jobs, chunksize=1):
            rows.extend(part)
    flag_duplicates(rows)
    return rows


def flag_duplicates(rows: list[dict]) -> None:
    """A trade 'also appears under another event' when another event's book
    for the same symbol and bar size took the same entry (Dean, answer 11)."""
    seen = defaultdict(set)
    for r in rows:
        seen[(r["symbol"], r["timeframe"], r["strategy"], r["direction"], r["entry_time"])].add(r["event_id"])
    for r in rows:
        others = seen[(r["symbol"], r["timeframe"], r["strategy"], r["direction"], r["entry_time"])] - {r["event_id"]}
        r["also_under"] = "; ".join(sorted(others))
