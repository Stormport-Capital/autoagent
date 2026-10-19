"""Step C: exchange and security type for every PENDING symbol, one FMP
profile call each (approved budget: Dean, second round answer 1).

    python security_master.py --max-calls 407

Resumable: raw profiles are cached in cache/profiles/; a symbol already
cached costs no call. Writes inputs/security_master.csv, then re-run
`python events.py` to apply it.

Classification (stated, not inferred): ETF if isEtf, fund if isFund;
warrant / unit / right / preferred when the company name says so; everything
else common. FMP's profile exchange is the CURRENT listing, not the listing on
the event date. Every non-common classification and every exchange value
outside NASDAQ / NYSE / AMEX is printed for review.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter

import events
import spec
from fmp import Fmp

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(HERE, "cache", "profiles")
OUT = os.path.join(HERE, "inputs", "security_master.csv")
EXCHANGE_ALIASES = {"NYSE AMERICAN": "AMEX", "NYSEAMERICAN": "AMEX", "NYSE MKT": "AMEX"}
NAME_TYPES = (("warrant", r"\bwarrants?\b"), ("unit", r"\bunits?\b"), ("right", r"\brights?\b"),
              ("preferred", r"\bpreferred\b|\bpfd\b"))


def pending_symbols() -> list[str]:
    rows, splits, sessions = events.load_candidates(), events.load_splits(), events.load_sessions()
    syms = set()
    for name in spec.ACTIVE:
        for e in events.build(spec.SPECS[name], rows, splits, sessions, None):
            if e["status"] == "PENDING":
                syms.add(e["symbol"])
                syms.update(x for x in e["same_bars_as"].split(";") if x)
    return sorted(syms)


def classify(p: dict) -> str:
    if p.get("isEtf"):
        return "etf"
    if p.get("isFund"):
        return "fund"
    name = (p.get("companyName") or "").lower()
    for kind, pat in NAME_TYPES:
        if re.search(pat, name):
            return kind
    return "common"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-calls", type=int, required=True)
    args = ap.parse_args()
    os.makedirs(PROFILES, exist_ok=True)
    syms = pending_symbols()
    todo = [s for s in syms if not os.path.exists(os.path.join(PROFILES, f"{s}.json"))]
    print(f"{len(syms)} symbols need a profile; {len(syms) - len(todo)} cached; {len(todo)} to fetch")
    if len(todo) > args.max_calls:
        raise SystemExit(f"STOP: {len(todo)} profile calls needed but the budget is {args.max_calls}. "
                         "Ask Dean before raising it.")
    fmp = Fmp(args.max_calls)
    for s in todo:
        body, _n = fmp.profile(s)
        with open(os.path.join(PROFILES, f"{s}.json"), "w") as f:
            json.dump(body, f)
    rows, odd_exch, non_common = [], Counter(), []
    for s in syms:
        with open(os.path.join(PROFILES, f"{s}.json")) as f:
            body = json.load(f)
        p = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) and body else None)
        if not p:
            rows.append({"symbol": s, "profile_found": "no"})
            continue
        exch = (p.get("exchange") or "").upper()
        exch = EXCHANGE_ALIASES.get(exch, exch)
        kind = classify(p)
        if exch not in spec.EXCHANGES:
            odd_exch[exch] += 1
        if kind != "common":
            non_common.append((s, kind, p.get("companyName")))
        rows.append({"symbol": s, "profile_found": "yes", "exchange": exch,
                     "exchange_full": p.get("exchangeFullName", ""), "security_type": kind,
                     "is_etf": p.get("isEtf"), "is_fund": p.get("isFund"), "is_adr": p.get("isAdr"),
                     "is_actively_trading": p.get("isActivelyTrading"), "ipo_date": p.get("ipoDate") or "",
                     "company_name": p.get("companyName", "")})
    fields = ["symbol", "profile_found", "exchange", "exchange_full", "security_type", "is_etf", "is_fund",
              "is_adr", "is_actively_trading", "ipo_date", "company_name"]
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"FMP calls this run: {fmp.calls}; wrote {OUT}")
    print("no profile:", sum(r["profile_found"] == "no" for r in rows))
    print("exchanges outside NASDAQ/NYSE/AMEX:", dict(odd_exch))
    print("non-common classifications (review):")
    for s, kind, name in non_common:
        print(f"  {s:8} {kind:9} {name}")


if __name__ == "__main__":
    main()
