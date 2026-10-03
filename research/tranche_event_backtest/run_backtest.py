"""Step E.2-E.4: run the backtest on the pulled bars and build the outputs.

    python run_backtest.py                 # run (resumes), then workbook + HTML + checks
    python run_backtest.py --report-only   # rebuild outputs from saved results

Needs: inputs/security_master.csv (Step C), out/probe.json (Step D) and
cache/bars/<SYMBOL>.csv.gz (fetch_bars.py). Writes:
  cache/event_bars/  one bar file per event, on one price basis
  cache/results/     one result file per event x bar size (resume points)
  out/trades.csv, out/tranche_event_backtest.xlsx, out/summary.html,
  out/hand_check_picks.csv, out/extend.json (when trades are still open)

Per event (decisions in HANDOFF.md):
* window = 35 calendar days before day 0 through day 25 + 60 sessions
  (+60 more per extension), capped at the last bar;
* as-traded bars with a split inside the window are put on the basis of the
  window's last day (intraday.one_basis); adjusted bars are used as they are;
* INTRADAY_100: the signal bar is the first 5-minute bar of day 0, pre-market
  or regular, whose high reaches 2x the prior regular close (gold's as-traded
  close, carried to the bars' basis); its as-traded close must be >= $1;
  the engine's added_at is the later of that bar's end and 09:30;
* CLOSE_100: added_at = day 0 16:00.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
from collections import Counter
from datetime import date, timedelta

import events
import html_summary
import intraday
import replay
import report
import runner
import spec
from indicators import in_rth

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "out")
STATE = os.path.join(OUT, "extend_state.json")   # event_id -> extensions so far
SEED = 20261002                                   # fixed: the same 5 hand-check picks on every rebuild


def split_factors(splits, sym) -> list[tuple[date, float]]:
    return [(d, float(den / num)) for d, num, den in splits.get(sym, [])]


def carry(factors, after: date, through: date) -> float:
    """Product of split factors effective in (after, through]: multiplies an
    as-traded price on `after` onto the basis of `through`."""
    k = 1.0
    for d, f in factors:
        if after < d <= through:
            k *= f
    return k


def basis() -> str:
    with open(os.path.join(OUT, "probe.json")) as f:
        reading = (json.load(f).get("call1_split") or {})
    reading = reading.get("reading", "") if isinstance(reading, dict) else ""
    if reading.startswith("as-traded"):
        return "as_traded"
    if reading.startswith("split-adjusted"):
        return "adjusted"
    raise SystemExit(f"STOP: probe.json does not say whether bars are as-traded or adjusted ({reading!r})")


def prepare(sessions, data_end, extra) -> tuple[list[runner.EventRun], list[dict], list[dict]]:
    rows, splits = events.load_candidates(), events.load_splits()
    master = events.load_master()
    if master is None:
        raise SystemExit("STOP: inputs/security_master.csv missing (run security_master.py)")
    kind = basis()
    runs, tested, dropped = [], [], []
    os.makedirs(os.path.join(CACHE, "event_bars"), exist_ok=True)
    loaded = {}
    for name in spec.ACTIVE:
        ev = spec.SPECS[name]
        for e in events.build(ev, rows, splits, sessions, master):
            if e["status"] != "TESTED":
                continue
            sym, d0 = e["symbol"], date.fromisoformat(e["event_date"])
            eid = f"{name}:{sym}:{d0}"
            path = os.path.join(CACHE, "bars", f"{sym}.csv.gz")
            if not os.path.exists(path):
                dropped.append({**e, "status": "EXCLUDED", "reason": "no 5-minute bars on file"})
                continue
            if sym not in loaded:
                loaded[sym] = intraday.load_bars(path)
            i = sessions.index(d0)
            j = i + spec.ENTRY_LAST_DAY + spec.EXIT_EXTRA_SESSIONS * (1 + extra.get(eid, 0))
            end = min(sessions[j] if j < len(sessions) else data_end, data_end)
            start = d0 - timedelta(days=spec.ENGINE_WINDOW_DAYS)
            bars = [b for b in loaded[sym] if start <= b.session <= end]
            if not any(b.session == d0 for b in bars):
                dropped.append({**e, "status": "EXCLUDED", "reason": "no 5-minute bars on day 0"})
                continue
            facs = split_factors(splits, sym)
            applied = 0
            if kind == "as_traded":
                bars, applied = intraday.one_basis(bars, [(dd, f) for dd, f in facs])
                basis_end = bars[-1].start.date()
            else:
                basis_end = date.max          # FMP-adjusted to today
            if name == "INTRADAY_100":
                ref = float(e["ref_close_raw"]) * carry(facs, date.fromisoformat(e["ref_date"]), basis_end)
                sig = intraday.signal_bar(bars, d0, ref, ev.multiple)
                if sig is None:
                    dropped.append({**e, "status": "EXCLUDED",
                                    "reason": "no 5-minute bar of day 0 reached 2x the prior close"})
                    continue
                px = sig.close / carry(facs, d0, basis_end)
                if px < spec.MIN_PRICE:
                    dropped.append({**e, "status": "EXCLUDED",
                                    "reason": f"price < $1 at the signal bar close ({px:.4f}, as-traded)"})
                    continue
                signal_time, added = sig.end, replay.signal_added_at(sig.end)
                sig_price = px
            else:
                signal_time = added = replay.close_added_at(d0)
                sig_price = float(e["close_raw"])
            bp = os.path.join(CACHE, "event_bars", f"{eid.replace(':', '_')}.csv.gz")
            intraday.save_bars(bp, bars)
            runs.append(runner.EventRun(eid, sym, name, d0, e["held_faded"], signal_time, added, bp, applied))
            tested.append({**e, "event_id": eid, "signal_time": signal_time.replace(tzinfo=None),
                           "signal_price_as_traded": round(sig_price, 6), "splits_rescaled_in_window": applied,
                           "window_end": end})
    return runs, tested, dropped


def all_sessions() -> tuple[list[date], date]:
    """Trading sessions: gold's SPY calendar (inputs/sessions_spy.txt), plus any
    later session that appears in the pulled bars (regular hours), up to the
    last session any bar file reaches (the data end)."""
    gold = set(events.load_sessions())
    seen, data_end = set(), None
    for f in os.listdir(os.path.join(CACHE, "bars")):
        for b in intraday.load_bars(os.path.join(CACHE, "bars", f)):
            if b.session > max(gold) and in_rth(b.start):
                seen.add(b.session)
            data_end = b.session if data_end is None or b.session > data_end else data_end
    return sorted(d for d in gold | seen if d <= data_end), data_end


def flat_trade(r) -> dict:
    out = {k: v for k, v in r.items() if k != "legs"}
    for n, leg in enumerate(r["legs"][:2], start=1):
        out[f"leg{n}_time"], out[f"leg{n}_price"], out[f"leg{n}_qty"] = leg["time"], leg["price"], leg["qty"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--processes", type=int, default=None)
    ap.add_argument("--check-formulas", action="store_true",
                    help="recalculate the workbook in LibreOffice and compare every formula with Python")
    args = ap.parse_args()
    sessions, data_end = all_sessions()
    extra = json.load(open(STATE)) if os.path.exists(STATE) else {}
    runs, tested, dropped = prepare(sessions, data_end, extra)
    results = os.path.join(CACHE, "results")
    rows = runner.run_all(runs, sessions, data_end, intraday.load_bars, processes=args.processes,
                          results_dir=results, extra=extra)

    # trades still open with more data available: extend their window by 60 sessions
    want, ext = {}, dict(extra)
    ends = {t["event_id"]: t["window_end"] for t in tested}
    for r in rows:
        if r["status"] == "OPEN" and ends[r["event_id"]] < data_end and not args.report_only:
            ext[r["event_id"]] = extra.get(r["event_id"], 0) + 1
    if ext != extra:
        for eid in {k for k in ext if ext[k] != extra.get(k, 0)}:
            for book in runner.BOOK_ORDER:
                p = os.path.join(results, f"{eid.replace(':', '_')}__{book}.pkl")
                if os.path.exists(p):
                    os.remove(p)
            sym, cur_end = eid.split(":")[1], ends[eid]
            i = sessions.index(cur_end)
            to = sessions[min(len(sessions) - 1, i + spec.EXIT_EXTRA_SESSIONS)]
            a, b = want.get(sym, (cur_end, to))
            want[sym] = (min(a, cur_end), max(b, to))
        with open(STATE, "w") as f:
            json.dump(ext, f, indent=1)
        with open(os.path.join(OUT, "extend.json"), "w") as f:
            json.dump({s: [a.isoformat(), b.isoformat()] for s, (a, b) in want.items()}, f, indent=1)
        print(f"{len(want)} symbols have trades still open: run `python fetch_bars.py --max-calls N --extend`, "
              "then this script again")
        return

    excluded = []
    for name in spec.ACTIVE:
        with open(os.path.join(OUT, f"excluded_{name}.csv")) as f:
            excluded += list(csv.DictReader(f))
    excluded += dropped
    closed = [r for r in rows if r["status"] == "CLOSED"]
    picks = random.Random(SEED).sample(closed, k=min(5, len(closed)))
    with open(os.path.join(OUT, "hand_check_picks.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "timeframe", "strategy", "direction", "entry_time"])
        for r in picks:
            w.writerow([r["event_id"], r["timeframe"], r["strategy"], r["direction"], r["entry_time"].isoformat()])
    hand = os.path.join(OUT, "hand_checks.md")
    dup = sum(bool(r["also_under"]) for r in rows)
    recon = list(csv.DictReader(open(os.path.join(OUT, "reconciliation.csv"))))
    checks = ["Count reconciliation (events.py, then intraday-stage exclusions)"]
    checks += [[r["event_type"], r["bucket"], r["reason"], int(r["events"]), int(r["symbols"])] for r in recon]
    for (et, why), n in sorted(Counter((d["event_type"], d["reason"].split(" (")[0]) for d in dropped).items()):
        checks.append([et, "EXCLUDED at intraday stage", why, n, ""])
    for et in spec.ACTIVE:
        checks.append([et, "TESTED", "", sum(t["event_type"] == et for t in tested), ""])
    checks += ["", f"Trades that also appear under another event: {dup} of {len(rows)}",
               f"Events whose bar window was rescaled for a split: {sum(t['splits_rescaled_in_window'] > 0 for t in tested)}",
               "", "Hand-verified trades (out/hand_checks.md):"]
    checks += open(hand).read().splitlines() if os.path.exists(hand) else ["NOT DONE YET - see HANDOFF.md"]
    with open(os.path.join(OUT, "trades.csv"), "w", newline="") as f:
        flat = [flat_trade(r) for r in rows]
        if flat:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in flat for k in r)))
            w.writeheader()
            w.writerows(flat)
    report.write_workbook(os.path.join(OUT, "tranche_event_backtest.xlsx"), rows, tested, excluded, checks)
    meta = (f"{len(tested)} tested events, {len(rows)} trades, data through {data_end}. "
            "Net R at the engine's default costs (5 bps per side, 10%/yr borrow / 360). "
            "The workbook recalculates net R from its Assumptions tab.")
    html_summary.write_html(os.path.join(OUT, "summary.html"), rows, meta)
    print(meta)
    if args.check_formulas:
        import tempfile
        import selftest
        selftest.check_workbook(rows, tempfile.mkdtemp(), os.path.join(OUT, "tranche_event_backtest.xlsx"))


if __name__ == "__main__":
    main()
