"""Step 2: build the INTRADAY_100 and CLOSE_100 event lists.

Input: monthly exports of sql/candidates.sql (inputs/cand_YYYY-MM.csv), the
split list from sql/splits.sql (inputs/splits.csv), the session calendar
(inputs/sessions_spy.txt) and, when available, a security-master CSV
(inputs/security_master.csv: symbol,exchange,security_type) for the exchange
and common-stock filters.

Every candidate ends up in exactly one bucket: TESTED, EXCLUDED (with the
first failing reason) or SUSPECT (with every suspect reason). Events whose
exchange or security type is not yet known are marked PENDING_METADATA and
stay out of TESTED until a security master is supplied.

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

# Basis for the $1 and 10,000-share tests. Gold's own convention is "all
# calculations use adjusted prices" (migration 048), so adjusted is the default.
# Open question for Dean: as-traded instead? Both counts are reported.
PRICE_BASIS = os.environ.get("EVENT_PRICE_BASIS", "adjusted")    # adjusted | as_traded
VOLUME_BASIS = os.environ.get("EVENT_VOLUME_BASIS", "adjusted")  # adjusted | as_traded


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
    only known from intraday bars; until then the 2x level stands in for it."""
    if ev.basis == "close":
        px = r["close"] if PRICE_BASIS == "adjusted" else r["raw_close"]
    else:
        ref = r["ref_close"] if PRICE_BASIS == "adjusted" else r["ref_raw_close"]
        px = ref * D(str(ev.multiple))
    return px


def build(ev: spec.EventSpec, rows: list[dict], splits, sessions, master) -> list[dict]:
    session_set = set(sessions)
    mult = D(str(ev.multiple))
    base = [r for r in rows if ev.basis == "high" or r["close"] >= mult * r["ref_close"]]
    # identical bars under two tickers on the same day (FMP serves an old and a new
    # ticker with one history); which one was listed that day needs the master
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
            "avg_vol_20": vol, "volume_basis": VOLUME_BASIS, "prior_sessions": r["n_prior"],
            "splits_within_5d": ";".join(f"{a}:{n:g}-for-{m:g}" for a, n, m in near),
            "factor_step_on_day": r["factor"] != r["ref_factor"],
            "same_bars_as": ";".join(sorted(same[(r["trade_date"], r["open"], r["high"], r["low"],
                                                  r["close"], r["volume"])] - {sym})),
            "raw_symbol": r["symbol"],
            "exchange": "", "security_type": "",
            "suspect_many_events": False, "suspect_jump_no_ca": False,
            "possible_unapplied_split": False,
            "status": "", "reason": "",
        }
        if master is not None and sym in master:
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
        elif e["price_test"] < D(str(spec.MIN_PRICE)):
            what = "close" if ev.basis == "close" else "2x prior close (signal-bar close needs intraday bars)"
            e["status"], e["reason"] = "EXCLUDED", f"price < $1 ({what}, {PRICE_BASIS})"
        elif e["avg_vol_20"] is None or e["avg_vol_20"] < spec.MIN_AVG_VOLUME:
            e["status"], e["reason"] = "EXCLUDED", f"avg volume prior 20 sessions < 10,000 ({VOLUME_BASIS})"
        elif master is not None and e["exchange"] and e["exchange"] not in spec.EXCHANGES:
            e["status"], e["reason"] = "EXCLUDED", f"exchange {e['exchange']} not NASDAQ/NYSE/NYSE American"
        elif master is not None and e["security_type"] and e["security_type"] != "common":
            e["status"], e["reason"] = "EXCLUDED", f"not common stock ({e['security_type']})"

    # suspects among the events still standing
    live = [e for e in events if not e["status"]]
    per_symbol = Counter(e["symbol"] for e in live)
    for e in live:
        why = []
        if per_symbol[e["symbol"]] >= spec.SUSPECT_MIN_EVENTS:
            e["suspect_many_events"] = True
            why.append(f"symbol has {per_symbol[e['symbol']]} {ev.name} events in the period")
        if e["jump"] >= D(str(spec.SUSPECT_JUMP)) and not e["splits_within_5d"]:
            e["suspect_jump_no_ca"] = True
            why.append(f"jump {e['jump']:.2f}x with no recorded split within ±{spec.CA_WINDOW_DAYS} days")
        if why:
            e["status"], e["reason"] = "SUSPECT", "; ".join(why)
    for e in events:
        if e["status"]:
            continue
        pend = []
        if master is None or e["symbol"] not in master:
            pend.append("exchange and security type unknown (no security master)")
        if e["same_bars_as"]:
            pend.append(f"same bars as {e['same_bars_as']} on this day: listed ticker unknown")
        if e["splits_within_5d"] and not e["factor_step_on_day"] and e["jump"] >= D(str(spec.SUSPECT_JUMP)):
            e["possible_unapplied_split"] = True
            pend.append("split recorded within ±5 days but gold not adjusted: jump may be the split")
        e["status"], e["reason"] = ("PENDING", "; ".join(pend)) if pend else ("TESTED", "")
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
        write(os.path.join(OUT, f"excluded_{name}.csv"), [e for e in events if e["status"] in ("EXCLUDED", "SUSPECT")])
        c = Counter((e["status"], e["reason"].split(" (")[0] if e["status"] == "EXCLUDED" else e["status"]) for e in events)
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
