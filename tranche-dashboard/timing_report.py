"""Read-only timing report: how fast each check reached IBKR.

    sudo -u tranche /opt/tranche/venv/bin/python -I timing_report.py [YYYY-MM-DD]

Run from the deployed tranche-dashboard folder. It opens the dashboard's
databases read-only and prints, for that ET day (default today):

  * every IBKR order: seconds from the bar close to the order leaving, and
    to IBKR's fill;
  * per book: how many checks and retries ran, and the median and worst
    seconds from the bar close to the bars being fetched, the IBKR sync being
    done, and the check being done;
  * per symbol: when its 5-minute bars were first seen, and how many came in
    after the 5-min book's check time;
  * the Phase 1 verdict (PLAN_TIMEFRAMES.md): bar close to order sent under
    20 s.

Nothing is changed and nothing is sent anywhere.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from broker import seconds_after  # noqa: E402
from indicators import ET  # noqa: E402

BOOKS = (("1h", "Hourly", "tranche.db"), ("15m", "15-min", "tranche_15m.db"),
         ("5m", "5-min", "tranche_5m.db"))
TARGET_S = 20.0  # Phase 1 exit criterion: bar close to order sent


def _open(path: Path):
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _rows(db, sql: str, args=()) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:  # a database from before the timing record
        return []


def _stats(values: list) -> str:
    v = sorted(x for x in values if x is not None)
    if not v:
        return "-"
    return f"median {v[len(v) // 2]:.1f} s, worst {v[-1]:.1f} s"


def _delay_s(db) -> float | None:
    r = _rows(db, "SELECT value FROM settings WHERE key='bar_close_delay_min'")
    try:
        return float(r[0]["value"]) * 60 if r else 120.0  # the store's default is 2 minutes
    except (TypeError, ValueError):
        return None


def report(folder: Path, day: str) -> str:
    out = [f"Timing report for {day} (seconds after the bar close; the 9:30 open for "
           f"the last bar of the previous day)", ""]
    like = day + "%"
    live = _open(folder / "tranche_live.db")
    sent = []

    out.append("IBKR orders")
    orders = _rows(live, "SELECT * FROM orders WHERE bar_end LIKE ? ORDER BY id", (like,)) if live else []
    if not orders:
        out.append("  none")
    for o in orders:
        s = seconds_after(o["bar_end"], o["sent_at"])
        f = seconds_after(o["bar_end"], o["filled_at"])
        sent.append(s)
        out.append(f"  {o['bar_end'][11:16]}  {o['symbol']:<6} {o['side']:<4} {o['qty']}  "
                   f"{o['status']:<9} sent {'-' if s is None else f'{s:.1f} s':>7}  "
                   f"filled {'-' if f is None else f'{f:.1f} s':>7}"
                   + (f"  ({o['message'][:70]})" if o.get("message") else ""))
    out.append("")

    out.append("Checks per book")
    five = None
    for key, label, name in BOOKS:
        db = _open(folder / name)
        if db is None:
            continue
        if key == "5m":
            five = db
        rows = _rows(db, "SELECT * FROM checks WHERE bar_end LIKE ? ORDER BY id", (like,))
        checks = [r for r in rows if r["kind"] == "check"]
        if not rows:
            out.append(f"  {label}: no checks recorded")
            continue
        waiting = sum(1 for r in checks if r["behind"])
        retries = sum(1 for r in rows if r["kind"] == "retry")
        stops = [r for r in rows if r["kind"] == "stop"]
        out.append(f"  {label}: {len(checks)} checks, {retries} retries"
                   + (f", {len(stops)} 5-minute stop checks" if stops else "")
                   + f"; {waiting} check(s) left a symbol waiting for its bar")
        out.append(f"    bars fetched  {_stats([seconds_after(r['bar_end'], r['data_at']) for r in checks])}")
        ibkr = [seconds_after(r["bar_end"], r["ibkr_done"]) for r in checks if r["ibkr_done"]]
        if ibkr:
            out.append(f"    IBKR done     {_stats(ibkr)}")
        out.append(f"    check done    {_stats([seconds_after(r['bar_end'], r['done_at']) for r in checks])}")
    out.append("")

    out.append("5-minute bars, first seen (per symbol)")
    src = live if live is not None else five
    arr = _rows(src, "SELECT * FROM bar_arrivals WHERE bar_end LIKE ? ORDER BY symbol, bar_end",
                (like,)) if src is not None else []
    delay = _delay_s(five) if five is not None else None
    by_sym: dict[str, list] = {}
    for r in arr:
        by_sym.setdefault(r["symbol"], []).append(seconds_after(r["bar_end"], r["first_seen_at"]))
    if not by_sym:
        out.append("  none recorded")
    for sym, lags in sorted(by_sym.items()):
        late = "" if delay is None else (
            f"; {sum(1 for x in lags if x is not None and x > delay + 1)} after the "
            f"{delay:.0f} s check")
        out.append(f"  {sym:<6} {len(lags)} bars, {_stats(lags)}{late}")
    out.append("")

    good = [x for x in sent if x is not None]
    if good:
        worst = max(good)
        verdict = "PASS" if worst < TARGET_S else "FAIL"
        out.append(f"Phase 1 verdict: {verdict} - {len(good)} order(s) sent, worst "
                   f"{worst:.1f} s after the bar close (target under {TARGET_S:.0f} s)")
    else:
        out.append("Phase 1 verdict: not yet - no IBKR order was sent after a bar check that day")
    return "\n".join(out)


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).date().isoformat()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        sys.exit("usage: timing_report.py [YYYY-MM-DD]")
    print(report(HERE, day))


if __name__ == "__main__":
    main()
