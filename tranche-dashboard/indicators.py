"""Pure indicator math: hourly resampling, EMA, ATR, session VWAP, crosses.

Everything here is side-effect free so it can be unit-tested without a network,
a database or a clock. All timestamps are timezone-aware America/New_York.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


@dataclass(frozen=True)
class Bar:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def session(self) -> date:
        return self.start.astimezone(ET).date()


def in_rth(ts: datetime) -> bool:
    t = ts.astimezone(ET).time()
    return RTH_OPEN <= t < RTH_CLOSE


def bar_bucket(ts: datetime, minutes: int = 60) -> tuple[datetime, datetime]:
    """Clock-aligned bucket, as on standard charts. Hourly: the first bar is the
    half hour 9:30-10:00, then 10-11 ... 15-16. 15-minute: 9:30-9:45 ... 15:45-16:00."""
    local = ts.astimezone(ET)
    day = local.date()
    midnight = datetime.combine(day, time(0, 0), tzinfo=ET)
    mins = local.hour * 60 + local.minute
    start = midnight + timedelta(minutes=mins - mins % minutes)
    open_dt = datetime.combine(day, RTH_OPEN, tzinfo=ET)
    close_dt = datetime.combine(day, RTH_CLOSE, tzinfo=ET)
    return max(start, open_dt), min(start + timedelta(minutes=minutes), close_dt)


def hourly_bucket(ts: datetime) -> tuple[datetime, datetime]:
    return bar_bucket(ts, 60)


def session_bar_ends(day: date, minutes: int = 60) -> list[datetime]:
    """End times of a session's RTH bars (hourly: 10:00, 11:00, ..., 16:00)."""
    ends, t = [], datetime.combine(day, RTH_OPEN, tzinfo=ET)
    close_dt = datetime.combine(day, RTH_CLOSE, tzinfo=ET)
    while t < close_dt:
        t = bar_bucket(t, minutes)[1]
        ends.append(t)
    return ends


def session_hour_ends(day: date) -> list[datetime]:
    return session_bar_ends(day, 60)


def is_final_bar(b: "Bar") -> bool:
    return b.end.astimezone(ET).time() == RTH_CLOSE


def resample_hourly(bars: list[Bar]) -> list[Bar]:
    return resample(bars, 60)


def resample(bars: list[Bar], minutes: int = 60) -> list[Bar]:
    """Aggregate 5-minute bars into clock-aligned RTH bars of `minutes`."""
    out: list[Bar] = []
    cur: dict | None = None
    for b in sorted(bars, key=lambda x: x.start):
        if not in_rth(b.start):
            continue
        start, end = bar_bucket(b.start, minutes)
        if cur is None or cur["start"] != start:
            if cur is not None:
                out.append(Bar(**cur))
            cur = dict(start=start, end=end, open=b.open, high=b.high,
                       low=b.low, close=b.close, volume=b.volume)
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close
            cur["volume"] += b.volume
    if cur is not None:
        out.append(Bar(**cur))
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    """Exponential moving average seeded with the SMA of the first n values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def atr(bars: list[Bar], n: int) -> list[float | None]:
    """Wilder's average true range."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(bars)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def session_vwap(intraday: list[Bar], session: date, upto: datetime) -> float | None:
    """Cumulative RTH VWAP (typical price x volume) for `session` through `upto`."""
    pv = vol = 0.0
    for b in intraday:
        if b.session != session or not in_rth(b.start) or b.end > upto:
            continue
        pv += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
    return pv / vol if vol > 0 else None


def daily_closes(intraday: list[Bar]) -> dict[date, float]:
    """Last RTH close of each session."""
    out: dict[date, float] = {}
    for b in sorted(intraday, key=lambda x: x.start):
        if in_rth(b.start):
            out[b.session] = b.close
    return out


def daily_sma(closes: dict[date, float], session: date, n: int) -> float | None:
    """n-day simple average of the closes of the n sessions before `session`
    (shifted one day, so it is constant through the session)."""
    prior = [closes[d] for d in sorted(closes) if d < session][-n:]
    return sum(prior) / n if len(prior) == n else None


def crossed_below(a_prev, b_prev, a, b) -> bool:
    if None in (a_prev, b_prev, a, b):
        return False
    return a_prev >= b_prev and a < b


def crossed_above(a_prev, b_prev, a, b) -> bool:
    if None in (a_prev, b_prev, a, b):
        return False
    return a_prev <= b_prev and a > b
