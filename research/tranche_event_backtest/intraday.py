"""Intraday helpers that sit between the event lists and the engine replay:
the INTRADAY_100 signal bar, and putting one event's bar window on one price
basis.

Neither is a trading rule: the signal bar decides when the symbol is "added"
(the engine's own entry gate); the basis adjustment only rescales stored bars
so a split inside the window does not look like a price move (Dean, answer 10).
"""

from __future__ import annotations

import csv
import gzip
import os
from datetime import date, datetime, time, timedelta

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
from indicators import ET, Bar

FIVE = timedelta(minutes=5)
PREMARKET_FROM = time(4, 0)
RTH_CLOSE = time(16, 0)


def load_bars(path: str) -> list[Bar]:
    """Cached 5-minute bars: start (New York time, bar start), o, h, l, c, v."""
    out = []
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            start = datetime.fromisoformat(r["start"]).replace(tzinfo=ET)
            out.append(Bar(start, start + FIVE, float(r["open"]), float(r["high"]),
                           float(r["low"]), float(r["close"]), float(r["volume"] or 0)))
    out.sort(key=lambda b: b.start)
    return out


def save_bars(path: str, bars: list[Bar]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "open", "high", "low", "close", "volume"])
        for b in sorted(bars, key=lambda x: x.start):
            w.writerow([b.start.strftime("%Y-%m-%dT%H:%M:%S"), b.open, b.high, b.low, b.close, b.volume])


def signal_bar(bars: list[Bar], day0: date, prior_close: float, multiple: float) -> Bar | None:
    """First 5-minute bar of day 0, pre-market or regular hours, whose high
    reaches `multiple` x the prior regular close (Dean, answer 9). After-hours
    bars do not count."""
    level = multiple * prior_close
    for b in bars:
        t = b.start.astimezone(ET)
        if t.date() != day0 or t.time() < PREMARKET_FROM or t.time() >= RTH_CLOSE:
            continue
        if b.high >= level:
            return b
    return None


def one_basis(bars: list[Bar], splits: list[tuple[date, float]]) -> tuple[list[Bar], int]:
    """Put as-traded bars on the basis of the window's last day: every bar
    before a split's effective date has its prices multiplied by the split
    factor and its volume divided by it. `splits` holds (effective date,
    factor) with factor = denominator / numerator of core.corporate_actions,
    the same convention as gold's cum_split_factor (migration 048): a 1-for-10
    reverse split is (date, 10.0), a 2-for-1 split (date, 0.5). Returns the
    rescaled bars and how many splits were applied. Use only on as-traded bars;
    bars that are already split-adjusted need nothing."""
    if not splits or not bars:
        return bars, 0
    first, last = bars[0].start.date(), bars[-1].start.date()
    inside = [(d, r) for d, r in splits if first < d <= last]
    if not inside:
        return bars, 0
    out = []
    for b in bars:
        k = 1.0
        for d, r in inside:
            if b.start.astimezone(ET).date() < d:
                k *= r
        if k == 1.0:
            out.append(b)
        else:
            out.append(Bar(b.start, b.end, b.open * k, b.high * k, b.low * k, b.close * k, b.volume / k))
    return out, len(inside)
