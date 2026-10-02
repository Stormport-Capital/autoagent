"""Step 2: build the INTRADAY_100 and CLOSE_100 event lists.

Input: monthly exports of sql/candidates.sql (inputs/cand_YYYY-MM.csv), the
split list from sql/splits.sql (inputs/splits.csv), the session calendar
(inputs/sessions_spy.txt) and, when available, a security-master CSV
(inputs/security_master.csv: symbol,exchange,security_type) for the exchange
and common-stock filters.

Every candidate ends up in exactly one bucket: TESTED, EXCLUDED (with the
first failing reason) or PENDING (exchange / security type not yet known, or
the listed ticker of a same-bars pair undetermined). PENDING events stay out
of TESTED until a security master is supplied. There are no suspect
exclusions (Dean, second round: the 2.5x and 3+ events rules are dropped; 3+
events and short history are flags on the Events tab).

    python events.py                # writes out/events_*.csv, out/excluded_*.csv,
                                    # out/reconciliation.csv
"""

from __future__ import annotations

import csv
import glob
import os
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal as D

import spec

HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, "inputs")
OUT = os.path.join(HERE, "out")

COLS = ("symbol,trade_date,source,open,high,low,close,raw_close,factor,volume,ref_close,"
        "ref_raw_close,ref_date,ref_factor,avgvol_adj,avgvol_raw,n_prior").split(",")
NUM = ("open", "high", "low", "close", "raw_close", "factor", "ref_close", "ref_raw_close",
       "ref_factor", "avgvol_adj", "avgvol_raw")

# Basis for the $1 and 10,000-share tests: as-traded (Dean, answer 5 of the
# second round). `adjusted` is kept only to reproduce the earlier counts.
PRICE_BASIS = os.environ.get("EVENT_PRICE_BASIS", "as_traded")    # as_traded | adjusted
VOLUME_BASIS = os.environ.get("EVENT_VOLUME_BASIS", "as_traded")  # as_traded | adjusted


def load_candidates(pattern: str = "cand_*.csv") -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(INPUTS, pattern))):
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) != len(COLS):
                    raise ValueError(f"{path}: bad row {line!r}")
                r = dict(zip(COLS, parts))
                for k in NUM:
                    r[k] = D(r[k]) if r[k] else None
                r["volume"] = int(r["volume"]) if r["volume"] else 0
                r["n_prior"] = int(r["n_prior"])
                rows.append(r)
    return rows


def load_splits() -> dict[str, list[tuple[date, D, D]]]:
    out = defaultdict(list)
    with open(os.path.join(INPUTS, "splits.csv")) as f:
        for line in f:
            p = line.strip().split(",")
            if len(p) >= 4 and p[0]:
                out[p[0]].append((date.fromisoformat(p[1]), D(p[2]), D(p[3])))
    return out


def load_sessions() -> list[date]:
    with open(os.path.join(INPUTS, "sessions_spy.txt")) as f:
        return [date.fromisoformat(x.strip()) for x in f if x.strip()]


def load_master() -> dict[str, dict] | None:
    path = os.path.join(INPUTS, "security_master.csv")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return {r["symbol"]: r for r in csv.DictReader(f)}


def price_at_signal(r: dict, ev: spec.EventSpec) -> D:
    """CLOSE events: the day's close. INTRADAY events: the signal bar's close is
    only known from intraday bars; until then the price at the 2x level stands
    in for it. As-traded = adjusted / that day's gold split factor (migration 048:
    adjusted = raw x cum_split_factor), so a split between the prior day and day
    0 does not distort it."""
    if ev.basis == "close":
        return r["close"] if PRICE_BASIS == "adjusted" else r["raw_close"]
    level = r["ref_close"] * D(str(ev.multiple))
    return level if PRICE_BASIS == "adjusted" else level / r["factor"]


def split_error(e: dict) -> bool:
    """Exclude an event as a split error ONLY when both hold (Dean, 2026-10-02,
    replacing the earlier rule): (a) a recorded split takes effect after the
    prior close and by the end of the event day (ref_date < action_date <=
    event_date), and (b) the jump is within SPLIT_ERROR_TOLERANCE (20%) of that
    split's ratio (denominator / numerator; a 1-for-6 split excludes jumps of
    4.8x to 7.2x). Every other event stays in the study."""
    return e["split_error_match"]


def _split_match(r: dict, sym_splits) -> tuple[str, list[D]]:
    """Splits effective after the prior close and by the end of the event day
    (ref_date < action_date <= event_date), and their ratios (denominator /
    numerator). With more than one such split the combined ratio is also tried."""
    ref_d, d0 = date.fromisoformat(r["ref_date"]), date.fromisoformat(r["trade_date"])
    hits = [(a, n, m) for a, n, m in sym_splits if ref_d < a <= d0 and n]
    if not hits:
        return "", []
    ratios = [m / n for _a, n, m in hits]
    if len(hits) > 1:
        prod = D(1)
        for x in ratios:
            prod *= x
        ratios.append(prod)
    return ";".join(f"{a}:{n:g}-for-{m:g}" for a, n, m in hits), ratios


def listed_ticker(group: set[str], day: date, master: dict | None) -> str | None:
    """Of several tickers carrying identical bars on `day`, the one listed that
    day (Dean, answer 4): the only one with a security-master profile whose IPO
    date is on or before `day`. None when that does not single one out."""
    if master is None:
        return None
    ok = [s for s in group if master.get(s, {}).get("profile_found") == "yes"
          and (not master[s].get("ipo_date") or master[s]["ipo_date"] <= day.isoformat())]
    return ok[0] if len(ok) == 1 else None


def build(ev: spec.EventSpec, rows: list[dict], splits, sessions, master) -> list[dict]:
    session_set = set(sessions)
    mult = D(str(ev.multiple))
    base = [r for r in rows if ev.basis == "high" or r["close"] >= mult * r["ref_close"]]
    # identical bars under two tickers on the same day (FMP serves an old and a new
    # ticker with one history)
    same = defaultdict(set)
    for r in rows:
        same[(r["trade_date"], r["open"], r["high"], r["low"], r["close"], r["volume"])].add(
            r["symbol"].strip())

    events = []
    for r in base:
        sym = r["symbol"].strip()
        d = date.fromisoformat(r["trade_date"])
        level = r["high"] if ev.basis == "high" else r["close"]
        jump = level / r["ref_close"]
        near = [s for s in splits.get(sym, []) if abs((s[0] - d).days) <= spec.CA_WINDOW_DAYS]
        vol = r["avgvol_adj"] if VOLUME_BASIS == "adjusted" else r["avgvol_raw"]
        tol = D(str(spec.SPLIT_ERROR_TOLERANCE))
        in_day, ratios = _split_match(r, splits.get(sym, []))
        rel = min((jump / x for x in ratios), key=lambda v: abs(v - 1)) if ratios else None
        e = {
            "symbol": sym, "event_type": ev.name, "event_date": r["trade_date"],
            "ref_date": r["ref_date"], "ref_close_adj": r["ref_close"],
            "ref_close_raw": r["ref_raw_close"], "open_adj": r["open"], "high_adj": r["high"],
            "low_adj": r["low"], "close_adj": r["close"], "close_raw": r["raw_close"],
            "split_factor": r["factor"], "ref_split_factor": r["ref_factor"],
            "jump": round(jump, 4),
            "held_faded": ("HELD" if r["close"] >= mult * r["ref_close"] else "FADED")
            if ev.basis == "high" else "",
            "price_test": round(price_at_signal(r, ev), 6), "price_basis": PRICE_BASIS,
            "avg_vol_20": round(vol, 1) if vol is not None else None, "volume_basis": VOLUME_BASIS,
            "prior_sessions": r["n_prior"],
            "short_history": r["n_prior"] < spec.AVG_VOLUME_SESSIONS,
            "splits_within_5d": ";".join(f"{a}:{n:g}-for-{m:g}" for a, n, m in near),
            "factor_step_on_day": r["factor"] != r["ref_factor"],
            "split_effective_on_event_day": in_day,
            "jump_over_split_ratio": round(rel, 4) if rel is not None else None,
            "split_error_match": rel is not None and (1 - tol) <= rel <= (1 + tol),
            "same_bars_as": ";".join(sorted(same[(r["trade_date"], r["open"], r["high"], r["low"],
                                                  r["close"], r["volume"])] - {sym})),
            "raw_symbol": r["symbol"],
            "exchange": "", "security_type": "",
            "events_in_list": 0, "symbol_3plus_events": False,
            "status": "", "reason": "",
        }
        if master is not None and master.get(sym, {}).get("profile_found") == "yes":
            e["exchange"] = master[sym].get("exchange", "")
            e["security_type"] = master[sym].get("security_type", "")
        events.append(e)

    # exclusions, first failing reason wins
    for e in events:
        d = date.fromisoformat(e["event_date"])
        if e["raw_symbol"] != e["symbol"]:
            e["status"], e["reason"] = "EXCLUDED", f"duplicate row: ticker {e['raw_symbol']!r} has trailing whitespace, same bars as {e['symbol']}"
        elif d not in session_set:
            e["status"], e["reason"] = "EXCLUDED", "event date is not a trading session (weekend/holiday row in gold)"
        elif split_error(e):
            e["status"], e["reason"] = "EXCLUDED", (f"data error: split error (split {e['split_effective_on_event_day']} "
                                                    f"takes effect on the event day; jump {e['jump']:.2f}x is "
                                                    f"{e['jump_over_split_ratio']:.2f} x the split ratio)")
        elif e["price_test"] < D(str(spec.MIN_PRICE)):
            what = "close" if ev.basis == "close" else "price at 2x prior close; signal-bar close is re-tested on intraday bars"
            e["status"], e["reason"] = "EXCLUDED", f"price < $1 ({what}, {PRICE_BASIS})"
        elif e["avg_vol_20"] is None or e["avg_vol_20"] < spec.MIN_AVG_VOLUME:
            e["status"], e["reason"] = "EXCLUDED", f"avg volume prior 20 sessions < 10,000 ({VOLUME_BASIS})"
        elif master is not None and e["exchange"] and e["exchange"] not in spec.EXCHANGES:
            e["status"], e["reason"] = "EXCLUDED", f"exchange {e['exchange']} not NASDAQ/NYSE/NYSE American"
        elif master is not None and e["security_type"] and e["security_type"] != "common":
            e["status"], e["reason"] = "EXCLUDED", f"not common stock ({e['security_type']})"
        elif e["same_bars_as"]:
            group = {e["symbol"], *e["same_bars_as"].split(";")}
            keep = listed_ticker(group, d, master)
            if keep is not None and keep != e["symbol"]:
                e["status"], e["reason"] = "EXCLUDED", f"same bars as {keep} on this day; {keep} was the listed ticker"

    for e in events:
        if e["status"]:
            continue
        pend = []
        if master is None or master.get(e["symbol"], {}).get("profile_found") != "yes":
            pend.append("exchange and security type unknown (no FMP profile)")
        if e["same_bars_as"] and listed_ticker({e["symbol"], *e["same_bars_as"].split(";")},
                                               date.fromisoformat(e["event_date"]), master) is None:
            pend.append(f"same bars as {e['same_bars_as']} on this day: listed ticker UNKNOWN")
        e["status"], e["reason"] = ("PENDING", "; ".join(pend)) if pend else ("TESTED", "")

    # flags, not exclusions (Dean, answers 6 and 7 of the second round)
    live = [e for e in events if e["status"] in ("TESTED", "PENDING")]
    per_symbol = Counter(e["symbol"] for e in live)
    for e in live:
        e["events_in_list"] = per_symbol[e["symbol"]]
        e["symbol_3plus_events"] = per_symbol[e["symbol"]] >= spec.FLAG_MIN_EVENTS
    return events


def write(path: str, rows: list[dict]) -> None:
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    rows = load_candidates()
    splits, sessions, master = load_splits(), load_sessions(), load_master()
    recon = []
    built = {}
    for name in spec.ACTIVE:
        ev = spec.SPECS[name]
        events = build(ev, rows, splits, sessions, master)
        built[name] = events
        write(os.path.join(OUT, f"events_{name}.csv"), [e for e in events if e["status"] in ("TESTED", "PENDING")])
        write(os.path.join(OUT, f"excluded_{name}.csv"), [e for e in events if e["status"] == "EXCLUDED"])
        c = Counter((e["status"], e["reason"].split(" (")[0].split(":")[0] if e["status"] == "EXCLUDED" else e["status"]) for e in events)
        recon.append({"event_type": name, "bucket": "CANDIDATES", "reason": "", "events": len(events),
                      "symbols": len({e["symbol"] for e in events})})
        for (st, why), n in sorted(c.items()):
            sub = [e for e in events if e["status"] == st and (st != "EXCLUDED" or e["reason"].startswith(why))]
            recon.append({"event_type": name, "bucket": st, "reason": why if st == "EXCLUDED" else "",
                          "events": n, "symbols": len({e["symbol"] for e in sub})})
    write(os.path.join(OUT, "reconciliation.csv"), recon)
    for r in recon:
        print(f"{r['event_type']:13} {r['bucket']:10} {r['events']:5} ev {r['symbols']:4} sym  {r['reason']}")
    keep = {n: {(e["symbol"], e["event_date"]) for e in built[n] if e["status"] in ("TESTED", "PENDING")}
            for n in spec.ACTIVE}
    both = keep["INTRADAY_100"] & keep["CLOSE_100"]
    print(f"in both lists (tested or pending): {len(both)}")


if __name__ == "__main__":
    main()
