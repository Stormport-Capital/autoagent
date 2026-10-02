"""Event definitions and filter thresholds for the tranche-dashboard event backtest.

Nothing here is a trading rule. The rules live in tranche-dashboard/engine.py
and are only called, never copied. This file says which days count as events
and which events are tested.

An event is "price reached MULTIPLE x the regular close LOOKBACK sessions
earlier". The run asked for now uses LOOKBACK=1, MULTIPLE=2 (up 100% in one
day). The same code is meant to be re-run later for LOOKBACK=2, MULTIPLE=2
(up 100% in 2 days) and LOOKBACK=5, MULTIPLE=4 (up 300% in 5 days); those are
defined below but not run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EventSpec:
    name: str        # e.g. INTRADAY_100
    basis: str       # "high": the day's high reached it; "close": the day closed there
    lookback: int    # sessions back to the reference close
    multiple: float  # price / reference close


SPECS = {
    "INTRADAY_100": EventSpec("INTRADAY_100", "high", 1, 2.0),
    "CLOSE_100": EventSpec("CLOSE_100", "close", 1, 2.0),
    # Defined for later runs; not run yet (Dean, 2026-10-02).
    "INTRADAY_100_2D": EventSpec("INTRADAY_100_2D", "high", 2, 2.0),
    "CLOSE_100_2D": EventSpec("CLOSE_100_2D", "close", 2, 2.0),
    "INTRADAY_300_5D": EventSpec("INTRADAY_300_5D", "high", 5, 4.0),
    "CLOSE_300_5D": EventSpec("CLOSE_300_5D", "close", 5, 4.0),
}
ACTIVE = ("INTRADAY_100", "CLOSE_100")

PERIOD_START = date(2025, 10, 1)
PERIOD_END = date(2026, 9, 30)

# Filters (Dean's brief, Step 2)
MIN_PRICE = 1.0                 # price at the signal time
MIN_AVG_VOLUME = 10_000         # average shares over the prior 20 sessions
AVG_VOLUME_SESSIONS = 20
EXCHANGES = ("NASDAQ", "NYSE", "AMEX")  # AMEX = NYSE American in FMP's naming
SUSPECT_MIN_EVENTS = 3          # a symbol with this many events in the period
SUSPECT_JUMP = 2.5              # a jump this large with no recorded corporate action
CA_WINDOW_DAYS = 5              # "recorded corporate action" = split within +/- this many calendar days

# Backtest windows (Dean, Step 4 and pull plan)
ENTRY_LAST_DAY = 25             # entries on trading days 0..25 after the event
EXIT_EXTRA_SESSIONS = 60        # bars pulled past day 25 so open trades can exit
ENGINE_WINDOW_DAYS = 35         # the live engine sees the last 35 calendar days of 5-min bars
