"""Run tranche-dashboard/engine.py on stored 5-minute bars.

This wrapper only feeds bars in and reads trades out. It does not copy or
change any rule:

* ReplayProvider hands the engine the same 5-minute bars the dashboard's FMP
  client would have had at each moment: bars that have closed by `now`, from
  the last 35 calendar days (tranche-dashboard/data.py:227-229).
* The clock is the dashboard's own check schedule, `app.boundaries`
  (app.py:150-162), plus its retry rule for bars the feed has not finished
  (`Scheduler.retry_due`, app.py:219-228).
* The entry window uses the engine's own gates: the symbol's `added_at`
  (engine.py:397, a fill must come after it) and pausing the symbol after
  trading day 25 (engine.py:665-667: no new entries, open trades still managed).
* Every event runs in its own fresh book (one SQLite store per event and bar
  size), long & short mode, grade C, zero slippage and zero borrow, so the
  engine's gross P&L / risk_dollars is gross R (borrow runs at a negligible
  probe rate only to read the engine's night count back; see trades.py). Costs are applied later in the
  workbook with the engine's formulas.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
import app  # the dashboard's own scheduler helpers
from engine import Engine
from indicators import ET, RTH_OPEN, Bar, in_rth
from store import Store

import spec
import trades

BOOKS = {"1h": 60, "15m": 15, "5m": 5}   # app.py:54-56
WINDOW = timedelta(days=spec.ENGINE_WINDOW_DAYS)
# zero slippage; borrow at a negligible probe rate so the engine's own night
# count can be read back (trades.py). Neither changes gross P&L or R.
ZERO_COST = {"slippage_bps": 0.0, "borrow_rate_pct": trades.BORROW_PROBE_PCT,
             "borrow_day_count": trades.BORROW_DAY_COUNT}


class ReplayProvider:
    """Stored 5-minute bars, served the way FmpProvider.five_min_bars serves
    them (data.py:224-229): closed by `now`, starting within 35 days."""

    name = "replay"

    def __init__(self, bars: list[Bar]):
        self.bars = sorted(bars, key=lambda b: b.start)
        self.starts = [b.start for b in self.bars]
        self.ends = [b.end for b in self.bars]

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        lo = bisect.bisect_left(self.starts, now - WINDOW)
        hi = bisect.bisect_right(self.ends, now)
        return self.bars[lo:hi]

    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        # the 9:30 print exists before its 5-minute bar closes (test_tranche.py ScriptedProvider)
        i = bisect.bisect_left(self.starts, datetime.combine(day, RTH_OPEN, tzinfo=ET))
        if i < len(self.bars) and self.bars[i].session == day and self.bars[i].start <= now \
                and in_rth(self.bars[i].start):
            return self.bars[i].open
        return None


@dataclass
class Run:
    symbol: str
    book: str
    added_at: datetime
    pause_before: date | None     # first session on which no new entries are allowed
    end_day: date                 # last session to simulate
    store: Store
    engine: Engine
    symbol_id: int


def run_event(symbol: str, bars: list[Bar], book: str, added_at: datetime,
              sessions: list[date], pause_before: date | None, end_day: date,
              db_path: str = ":memory:") -> Run:
    """Simulate one event in one book from `added_at` through `end_day`."""
    minutes = BOOKS[book]
    store = Store(db_path)
    store.save_settings(ZERO_COST)
    provider = ReplayProvider(bars)
    eng = Engine(store, provider, minutes)
    eng.add_symbols(symbol, "long_short", 1.0, added_at, grade="C")
    sym_id = store.q("SELECT id FROM symbols WHERE symbol=?", (symbol,))[0]["id"]
    delay = timedelta(minutes=store.settings()["bar_close_delay_min"])
    sched = app.Scheduler(eng, clock=None)   # only its retry rule is used; never started
    paused = False
    for day in sessions:
        if day < added_at.astimezone(ET).date():
            continue
        if day > end_day:
            break
        for boundary in app.boundaries(day, delay, minutes):
            if boundary <= added_at:
                continue
            if pause_before is not None and not paused and day >= pause_before:
                eng.set_status(sym_id, "paused", boundary)
                paused = True
            eng.tick(boundary)
            sched.last_tick = boundary
            now = boundary
            window = min(app.RETRY_WINDOW, timedelta(minutes=minutes))
            while True:
                now = now + app.RETRY_EVERY
                if now - boundary > window or not sched.retry_due(now, boundary):
                    break
                eng.tick(now)
                sched.last_tick = now
    return Run(symbol, book, added_at, pause_before, end_day, store, eng, sym_id)


def signal_added_at(signal_bar_end: datetime) -> datetime:
    """Dean, 2026-10-02 (answers 6 and 9): entries start with the first bar of
    this book that closes after the signal bar. A pre-market signal can't use
    yesterday's last bar at today's open, so it counts from the 9:30 open."""
    t = signal_bar_end.astimezone(ET)
    open_ = datetime.combine(t.date(), RTH_OPEN, tzinfo=ET)
    return max(t, open_)


def close_added_at(day0: date) -> datetime:
    """CLOSE_100: the signal exists at day 0's 16:00 close; a cross on day 0's
    last bar may fill at day 1's open (Dean, answer 6)."""
    return datetime.combine(day0, time(16, 0), tzinfo=ET)
