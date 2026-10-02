"""Read the engine's own trade records back out of a replay run.

One row per trade. A VWAP short that scaled out (half at the 10-day MA, the
rest at the 20-day, engine.py:567-588) is stored by the engine as two closed
tranches with the same entry; here it is one trade with both exit legs and a
combined R (Dean, answer 4).

Gross R = gross P&L / risk_dollars, the engine's own R (engine.py:819-820)
before borrow; the run uses zero slippage, so it is gross of all costs.
risk_dollars is qty x the entry's stop distance: the session high for VWAP
trades, the EMA sizing distance for EMA trades (Dean, answer 1).

The run charges a negligible borrow rate (BORROW_PROBE_PCT) only so the
engine's own night count and prior-close marks (engine.py:471-474, 504-507,
730-740) can be read back as a per-share "borrow base"; the workbook then
prices it at whatever rate Dean sets. It does not change gross P&L or R.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from datetime import date, datetime

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
from indicators import ET

BORROW_PROBE_PCT = 1e-6          # %/yr, per calendar night on the prior close
BORROW_DAY_COUNT = 360

STRATEGY = {
    ("EMA5_10", None): "5/10 EMA cross",
    ("EMA10_20", None): "10/20 EMA cross",
    ("VWAP", "fail"): "VWAP fail (Russo Trigger B)",
    ("VWAP", "open_fade"): "Opening fade",
}


def exit_kind(reason: str) -> str:
    if reason.startswith("stop "):
        return "stop"
    if reason.startswith("target 2:"):
        return "target 20-day MA"
    if reason.startswith("target:"):
        return "target 10-day MA"
    if reason.startswith("time stop"):
        return "time stop"
    if re.match(r"(5/10|10/20) EMA crossed (up|down)", reason):
        return "opposite cross"
    return reason.split(":")[0]


def ema_unit(store, sleeve: str, entry_time: str) -> str:
    """How the EMA sizing distance was set ('pct' of price or 'abs' dollars),
    read from the engine's own entry log (engine.py:636-641, 706-710)."""
    rows = store.q("SELECT message FROM events WHERE kind='entry' AND sleeve=? AND ts=?",
                   (sleeve, entry_time))
    msg = rows[0]["message"] if rows else ""
    return "pct" if "p75 adverse move" in msg else "abs" if "x ATR" in msg else "?"


def sessions_between(sessions: list[date], a: date, b: date) -> int:
    """Trading days from a to b: same day = 0 (Dean, answer 12)."""
    return bisect.bisect_right(sessions, b) - bisect.bisect_right(sessions, a)


def extract(run, sessions: list[date], mark: float) -> list[dict]:
    """`mark`: the last available price when the data ends (the close of the
    last regular-hours 5-minute bar simulated); open trades are valued there."""
    store = run.store
    last_price = mark
    groups = defaultdict(list)
    for t in store.q("SELECT * FROM tranches ORDER BY id"):
        groups[(t["sleeve"], t["side"], t["entry_time"], t["entry_price"])].append(t)
    out = []
    for (sleeve, side, entry_time, entry_price), legs in groups.items():
        trig = legs[0]["trigger"] if sleeve == "VWAP" else None
        qty = sum(t["qty"] for t in legs)
        risk = sum(t["risk_dollars"] for t in legs)
        per_share = risk / qty
        sign = 1 if side == "long" else -1
        closed = [t for t in legs if t["status"] == "closed"]
        opened = [t for t in legs if t["status"] == "open"]
        closed.sort(key=lambda t: t["exit_time"])
        gross = sum(t["gross_pnl"] for t in closed)
        if opened:  # still open when the data ends: marked at the last price
            gross += sum(sign * (last_price - t["entry_price"]) * t["qty"] for t in opened)
        entry_dt = datetime.fromisoformat(entry_time).astimezone(ET)
        exit_legs = [(datetime.fromisoformat(t["exit_time"]).astimezone(ET), t["exit_price"],
                      t["qty"], t["exit_reason"], t["borrow_fees"]) for t in closed]
        if opened:
            exit_legs.append((None, last_price, sum(t["qty"] for t in opened), "OPEN",
                              sum(t["borrow_fees"] for t in opened)))
        last_exit = exit_legs[-1][0]
        hold = sessions_between(sessions, entry_dt.date(), last_exit.date()) if last_exit else None
        unit = ema_unit(store, sleeve, entry_time) if sleeve != "VWAP" else "stop"
        row = {
            "strategy": STRATEGY[(sleeve, trig)], "timeframe": run.book, "direction": side,
            "entry_time": entry_dt, "entry_price": entry_price,
            "stop": entry_price - sign * per_share,      # initial stop / sizing distance
            "risk_per_share": per_share, "ema_unit": unit, "qty": qty,
            "status": "OPEN" if opened else "CLOSED",
            "exit_reason": "OPEN (data ended)" if opened and not closed else
                           " + ".join(exit_kind(r) for _, _, _, r, _ in exit_legs if r != "OPEN")
                           + (" + OPEN" if opened else ""),
            "gapped_stop": any("gapped through" in r for _, _, _, r, _ in exit_legs),
            "holding_days": hold,
            "gross_r": gross / risk if risk else None,
            "legs": [{"time": t, "price": p, "qty": q,
                      "borrow_base": fees / q * BORROW_DAY_COUNT / (BORROW_PROBE_PCT / 100)
                      if q else 0.0}
                     for t, p, q, _, fees in exit_legs],
        }
        out.append(row)
    return out


def net_r(row: dict, slip_pct: float = 0.05, borrow_pct: float = 10.0,
          day_count: int = BORROW_DAY_COUNT) -> float:
    """The engine's net R for this trade at the given costs, rebuilt from the
    zero-cost run: slippage moves every fill against the trade by slip_pct
    (engine.py:672-673, 713-719), which also moves the stop distance the R is
    measured from (engine.py:674-681); borrow is rate / day_count per calendar
    night on the prior close (engine.py:730-740). Same formula as the
    workbook's Trades!net R column."""
    s = slip_pct / 100
    sign = 1 if row["direction"] == "long" else -1
    entry = row["entry_price"] * (1 + sign * s)
    q = sum(leg["qty"] for leg in row["legs"])
    pnl = sum(leg["qty"] / q * sign * (leg["price"] * (1 - sign * s) - entry) for leg in row["legs"])
    borrow = borrow_pct / 100 / day_count * sum(leg["qty"] / q * leg["borrow_base"] for leg in row["legs"])
    if row["ema_unit"] == "stop":
        risk = abs(row["stop"] - entry)
    elif row["ema_unit"] == "pct":
        risk = row["risk_per_share"] / row["entry_price"] * entry
    else:
        risk = row["risk_per_share"]
    return (pnl - borrow) / risk
