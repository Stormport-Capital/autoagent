"""TradeZero vs the dashboard: do they match, and how is the live account
really doing? Read-only: it never sends an order or buys a locate.

    sudo -u tranche /opt/tranche/venv/bin/python -I tz_reconcile.py [--since YYYY-MM-DD]

Run from the deployed tranche-dashboard directory (its .env is read). The
same report is on the dashboard's TradeZero card (Reconcile).

What it compares:
  * TradeZero's own fills (its fill-level history: price, commission, fees),
    plus today's order book for fills the history hasn't posted yet;
  * the dashboard's order log for the TradeZero link (tranche_live.db);
  * TradeZero's cash ledger: locate fees, overnight borrow, other charges;
    today's locates from its locate history when the ledger hasn't posted them;
  * TradeZero's open positions and their unrealized P&L;
  * the model's 5/10 tranche behind each live trade, for live-vs-model prices.

And reports:
  * MATCH / mismatch per day, symbol and side (a fill only at TradeZero is a
    trade made outside the bot; one only on the dashboard is a fill TradeZero
    doesn't show);
  * every round trip with gross P&L, commissions and fees, locate and borrow
    charges and the net;
  * the totals and the account's equity change they explain.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# `python -I tz_reconcile.py` (the documented way to run it) leaves the script's
# own folder off sys.path, so its neighbours must be put there before importing
sys.path.insert(0, str(Path(__file__).resolve().parent))
from indicators import ET  # noqa: E402

PRICE_TOL = 0.005        # 0.5%: a fill price this far from TradeZero's is a mismatch
# ledger notes seen live: "Locate 100 BB @ 0.0045 per share", "Single-Use 100 POAS @ 0.0098 per
# share", "ONB 1.00000000 ADXN ($-0.11129000) for 11/19"
LOCATE_NOTE = re.compile(r"(?:locate|single[- ]use|pre[- ]?borrow)\s+(?:credit\s+)?([\d.]+)\s+"
                         r"([A-Z][A-Z0-9.\-]*)\s+@\s*\$?([\d.]+)", re.I)
BORROW_NOTE = re.compile(r"\bONB\s+([\d.]+)\s+([A-Z][A-Z0-9.\-]*)", re.I)


def _side(row: dict, gross_key: str | None = None) -> str:
    """'buy' or 'sell'. The sign of the gross proceeds decides when there is one
    (negative = cash out = a buy or a cover): TradeZero's live fill history
    labels covers in a way a "Buy"/"Sell" reading took for sells (2026-11-24:
    every cover counted as a sell). Otherwise the label: buy, cover, BC, BTC."""
    if gross_key and row.get(gross_key) not in (None, ""):
        g = _f(row.get(gross_key))
        if g:
            return "buy" if g < 0 else "sell"
    lab = str(row.get("side") or "").strip().lower()
    return "buy" if lab.startswith("b") or "cover" in lab else "sell"


def _fees(row: dict) -> float:
    """Every charge on a fill: gross minus net when TradeZero gives both
    (that includes the regulatory and venue fees its totalFees can leave out),
    otherwise commission + totalFees."""
    if row.get("grossProceeds") not in (None, "") and row.get("netProceeds") not in (None, ""):
        return round(abs(_f(row["grossProceeds"]) - _f(row["netProceeds"])), 4)
    return round(abs(_f(row.get("commission"))) + abs(_f(row.get("totalFees"))), 4)
TRANSFER = re.compile(r"deposit|withdraw|transfer|wire|ach|journal", re.I)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _day(v) -> date | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None
    return d.astimezone(ET).date() if d.tzinfo else d.date()


def tz_era_orders(live_store) -> list[dict]:
    """The dashboard's filled orders sent to TradeZero. The live store is
    shared with the IBKR link: a TradeZero order's id IS its clientOrderId,
    an IBKR order's id is IBKR's number."""
    return [dict(r) for r in live_store.q(
        "SELECT * FROM orders WHERE broker_order_id = client_order_id AND filled_qty > 0 "
        "ORDER BY id")]


def default_since(live_store) -> date | None:
    """The first ET day the dashboard traded at TradeZero."""
    rows = live_store.q("SELECT ts FROM orders WHERE broker_order_id = client_order_id "
                        "ORDER BY id LIMIT 1")
    return _day(rows[0]["ts"]) if rows else None


def _tz_fills(broker, since: date, today: date) -> tuple[list[dict], list[str]]:
    """TradeZero's fills since `since`; today's from the order book when the
    history has none for today yet (no fee detail there)."""
    notes = []
    days = (today - since).days + 1
    rows = [r for r in broker.fills_history(since, days)
            if not r.get("canceled") and str(r.get("securityType") or "Stock") == "Stock"]
    fills = [{"day": _day(r.get("tradeDate")), "time": str(r.get("execTime") or ""),
              "symbol": str(r.get("symbol") or "").upper(),
              "side": _side(r, "grossProceeds"),
              "qty": int(round(abs(_f(r.get("qty"))))), "price": _f(r.get("price")),
              "fees": _fees(r),
              "source": "TradeZero fill history"} for r in rows]
    labels = sorted({str(r.get("side")) for r in rows})
    if labels:
        notes.append(f"TradeZero's fill history labels sides as: {', '.join(labels)} "
                     f"(read by the sign of the proceeds)")
    fills = [x for x in fills if x["day"] and since <= x["day"] <= today and x["qty"]]
    if not any(x["day"] == today for x in fills):
        todays = [o for o in broker.today_orders() if _f(o.get("executed")) > 0
                  and str(o.get("securityType") or "Stock") == "Stock"]
        for o in todays:
            fills.append({"day": today, "time": str(o.get("lastUpdated") or "")[11:19],
                          "symbol": str(o.get("symbol") or "").upper(),
                          "side": _side(o),
                          "qty": int(round(_f(o.get("executed")))), "price": _f(o.get("priceAvg")),
                          "fees": 0.0, "source": "today's order book"})
        if todays:
            notes.append("today's fills come from TradeZero's order book: its fill history has "
                         "not posted them yet, so today's commissions and fees are not counted")
    fills.sort(key=lambda x: (x["day"], x["time"]))
    return fills, notes


def _charges(broker, since: date, today: date) -> tuple[list[dict], list[str]]:
    """Cash-ledger rows since `since`, each tagged with a symbol where its note
    names one; today's locates from the locate history if the ledger lacks them."""
    notes = []
    rows = broker.cash_history(since, (today - since).days + 1)
    out = []
    for r in rows:
        note, typ = str(r.get("note") or ""), str(r.get("cashType") or "")
        amt = round(_f(r.get("deposit")) - _f(r.get("withdraw")), 4)
        m = LOCATE_NOTE.search(note) or BORROW_NOTE.search(note)
        kind = ("transfer" if TRANSFER.search(typ) and not m else
                "locate" if "locate fee" in typ.lower() or LOCATE_NOTE.search(note) else
                "borrow" if "borrow" in typ.lower() or BORROW_NOTE.search(note) else "other")
        out.append({"day": _day(r.get("entryDate")), "type": typ, "note": note, "amount": amt,
                    "symbol": m.group(2).upper() if m else None, "kind": kind,
                    "source": "TradeZero cash ledger"})
    if not any(x["kind"] == "locate" and x["day"] == today for x in out):
        added = 0
        for r in broker.locate_history():
            if r.get("locateStatus") == 50 and _f(r.get("locateShares")) > 0:
                cost = round(_f(r.get("locatePrice")) * _f(r.get("filledShares")), 4)
                out.append({"day": today, "type": "Locate Fees (today, not yet in the ledger)",
                            "note": f"Locate {int(_f(r.get('filledShares')))} "
                                    f"{str(r.get('symbol')).upper()} @ {_f(r.get('locatePrice'))}",
                            "amount": -cost, "symbol": str(r.get("symbol") or "").upper(),
                            "kind": "locate", "source": "TradeZero locate history"})
                added += 1
        if added:
            notes.append("today's locate fees come from TradeZero's locate history: the cash "
                         "ledger has not posted them yet")
    return out, notes


def _round_trips(fills: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Group TradeZero's fills per symbol into trips that start and end flat."""
    trips, pos, cur = [], defaultdict(int), {}
    for f in fills:
        sym, signed = f["symbol"], f["qty"] if f["side"] == "buy" else -f["qty"]
        t = cur.get(sym)
        if t is None:
            t = cur[sym] = {"symbol": sym, "side": "short" if signed < 0 else "long",
                            "opened": f["day"], "open_time": f["time"], "closed": None,
                            "fills": [], "qty": 0}
        t["fills"].append(f)
        pos[sym] += signed
        t["qty"] = max(t["qty"], abs(pos[sym]))
        if pos[sym] == 0:
            t["closed"] = f["day"]
            trips.append(t)
            del cur[sym]
    trips += list(cur.values())  # still open
    for t in trips:
        sells = [f for f in t["fills"] if f["side"] == "sell"]
        buys = [f for f in t["fills"] if f["side"] == "buy"]
        vw = lambda fs: (sum(f["qty"] * f["price"] for f in fs) / sum(f["qty"] for f in fs)) if fs else None
        t["entry"] = vw(sells if t["side"] == "short" else buys)
        t["exit"] = vw(buys if t["side"] == "short" else sells) if t["closed"] else None
        t["gross"] = round(sum(f["qty"] * f["price"] * (1 if f["side"] == "sell" else -1)
                               for f in t["fills"]), 4) if t["closed"] else None
        t["fees"] = round(sum(f["fees"] for f in t["fills"]), 4)
        t["shares"] = sum(f["qty"] for f in (sells if t["side"] == "short" else buys))
    return trips, dict(pos)


def _dash_trips(dash_orders: list[dict]) -> dict[str, list[list[dict]]]:
    """Per symbol, in order, the dashboard's orders grouped flat to flat: the
    k-th group is the orders behind TradeZero's k-th trip in that symbol (the
    fills match, so the two sequences line up; no clock needed)."""
    pos, out = defaultdict(int), defaultdict(list)
    for o in dash_orders:
        if pos[o["symbol"]] == 0:
            out[o["symbol"]].append([])
        out[o["symbol"]][-1].append(o)
        q = int(round(_f(o["filled_qty"])))
        pos[o["symbol"]] += q if o["side"] == "buy" else -q
    return out


def _book_keys(order: dict) -> list[str]:
    return [k for k in (order.get("book") or "").split(",") if k]


def _model_for(trip: dict, order: dict, key: str, books: dict, used: set) -> dict | None:
    """The model's 5/10 tranche behind a live trip in one book: same symbol and
    side, entered closest to the dashboard order that opened that book's part
    of the trip (within 20 minutes), not already matched to another trip.
    Before 2026-11-24 it took the day's first tranche, so a second trade in a
    symbol on a day was compared with the first."""
    st = books.get(key)
    if st is None:
        return None
    when = datetime.fromisoformat(order["ts"])
    best = None
    for r in st.q("SELECT id, entry_time, entry_price, exit_price, exit_reason, status, stop_price "
                  "FROM tranches WHERE symbol=? AND sleeve='EMA5_10' AND side=? "
                  "ORDER BY entry_time", (trip["symbol"], trip["side"])):
        gap = abs((datetime.fromisoformat(r["entry_time"]) - when).total_seconds())
        if gap <= 1200 and (key, r["id"]) not in used and (best is None or gap < best[0]):
            best = (gap, r)
    if best is None:
        return None
    used.add((key, best[1]["id"]))
    return {"book": key, **{k: v for k, v in dict(best[1]).items() if k != "id"}}


def _attribute(trip: dict, orders: list[dict] | None, books: dict, used: set) -> None:
    """Which books a live trip belongs to and the model trade behind each:
    trip["book_shares"] {book: shares it opened} (an order serving two books
    is split evenly between them) and trip["models"] {book: model tranche}."""
    trip["book_shares"], trip["models"] = {}, {}
    opening = "sell" if trip["side"] == "short" else "buy"
    if (not orders or _day(orders[0]["ts"]) != trip["opened"] or orders[0]["side"] != opening):
        return
    for o in orders:
        keys = _book_keys(o)
        if o["side"] != opening or not keys:
            continue
        q = _f(o["filled_qty"]) / len(keys)
        for key in keys:
            if key not in trip["models"]:
                trip["models"][key] = _model_for(trip, o, key, books, used)
            trip["book_shares"][key] = round(trip["book_shares"].get(key, 0) + q, 4)


def _split_by_book(t: dict) -> None:
    """trip["by_book"]: each book's share of a trip (its shares over the trip's
    opening shares) of TradeZero's gross, costs and net, and what the model's
    own prices made on those shares (None until the model trade has closed)."""
    total = sum(t["book_shares"].values())
    sign = 1 if t["side"] == "short" else -1
    t["by_book"] = {}
    for key, sh in t["book_shares"].items():
        frac = sh / total if total else 0.0
        m = t["models"].get(key)
        mg = (round(sign * (m["entry_price"] - m["exit_price"]) * sh, 4)
              if t["closed"] and m and m.get("exit_price") is not None else None)
        t["by_book"][key] = {
            "shares": sh, "frac": round(frac, 4), "model": m,
            "gross": round(t["gross"] * frac, 4) if t["gross"] is not None else None,
            "fees": round(t["fees"] * frac, 4),
            "locate_borrow": round((t["locate"] + t["borrow"]) * frac, 4),
            "net": round(t["net"] * frac, 4) if t["net"] is not None else None,
            "model_gross": mg}


def _book_totals(closed: list[dict]) -> dict:
    """Per book, its share of the closed trips: TradeZero's results, and on the
    trips with a closed model trade, TradeZero's gross against the model's."""
    out = {}
    for t in closed:
        for key, b in t.get("by_book", {}).items():
            o = out.setdefault(key, {"trips": 0, "wins": 0, "gross_wins": 0, "gross": 0.0,
                                     "fees": 0.0, "locate_borrow": 0.0, "net": 0.0,
                                     "compared": 0, "live_gross_compared": 0.0, "model_gross": 0.0})
            o["trips"] += 1
            o["wins"] += b["net"] > 0
            o["gross_wins"] += b["gross"] > 0
            for k in ("gross", "fees", "locate_borrow", "net"):
                o[k] += b[k]
            if b["model_gross"] is not None:
                o["compared"] += 1
                o["live_gross_compared"] += b["gross"]
                o["model_gross"] += b["model_gross"]
    for o in out.values():
        for k in ("gross", "fees", "locate_borrow", "net", "live_gross_compared", "model_gross"):
            o[k] = round(o[k], 2)
    return out


def reconcile(broker, live_store, books: dict | None = None, since: date | None = None,
              today: date | None = None) -> dict:
    """The full comparison as a dict (the CLI prints it; the dashboard shows it).
    books: book key -> Store, for the live-vs-model prices."""
    if getattr(broker, "name", "") != "TradeZero":
        raise ValueError("reconciliation reads TradeZero's own records; the linked live broker "
                         "is not TradeZero")
    today = today or datetime.now(ET).date()
    since = since or default_since(live_store) or today
    notes = []
    fills, n1 = _tz_fills(broker, since, today)
    charges, n2 = _charges(broker, since, today)
    notes += n1 + n2
    dash = [o for o in tz_era_orders(live_store) if (_day(o.get("filled_at") or o["ts"]) or today) >= since]

    # ---- MATCH: per day, symbol and side
    groups = defaultdict(lambda: {"tz_qty": 0, "tz_value": 0.0, "dash_qty": 0, "dash_value": 0.0})
    for f in fills:
        g = groups[(f["day"], f["symbol"], f["side"])]
        g["tz_qty"] += f["qty"]
        g["tz_value"] += f["qty"] * f["price"]
    for o in dash:
        q = int(round(_f(o["filled_qty"])))
        g = groups[(_day(o.get("filled_at") or o["ts"]), o["symbol"], o["side"])]
        g["dash_qty"] += q
        g["dash_value"] += q * _f(o["filled_avg_price"])
    matches = []
    for (day, sym, side), g in sorted(groups.items(), key=lambda kv: (kv[0][0] or today, kv[0][1], kv[0][2])):
        tz_px = g["tz_value"] / g["tz_qty"] if g["tz_qty"] else None
        dash_px = g["dash_value"] / g["dash_qty"] if g["dash_qty"] else None
        if not g["dash_qty"]:
            status = "ONLY AT TRADEZERO (a trade made outside the bot?)"
        elif not g["tz_qty"]:
            status = "ONLY ON THE DASHBOARD (TradeZero shows no such fill)"
        elif g["tz_qty"] != g["dash_qty"]:
            status = "SHARES DIFFER"
        elif tz_px and dash_px and abs(tz_px - dash_px) / tz_px > PRICE_TOL:
            status = "PRICE DIFFERS"
        else:
            status = "MATCH"
        matches.append({"day": day.isoformat() if day else None, "symbol": sym, "side": side,
                        "tz_qty": g["tz_qty"], "tz_price": tz_px, "dash_qty": g["dash_qty"],
                        "dash_price": dash_px, "status": status})

    # ---- round trips with their charges
    trips, tz_pos = _round_trips(fills)
    used, models_used, dash_trips, nth = set(), set(), _dash_trips(dash), defaultdict(int)
    for t in trips:
        last = t["closed"] or today
        mine = [i for i, c in enumerate(charges) if i not in used and c["symbol"] == t["symbol"]
                and c["kind"] in ("locate", "borrow") and c["day"] and t["opened"] <= c["day"] <= last]
        used.update(mine)
        t["locate"] = round(-sum(charges[i]["amount"] for i in mine if charges[i]["kind"] == "locate"), 4)
        t["borrow"] = round(-sum(charges[i]["amount"] for i in mine if charges[i]["kind"] == "borrow"), 4)
        t["net"] = (round(t["gross"] - t["fees"] - t["locate"] - t["borrow"], 4)
                    if t["gross"] is not None else None)
        k = nth[t["symbol"]]
        nth[t["symbol"]] += 1
        groups = dash_trips.get(t["symbol"], [])
        _attribute(t, groups[k] if k < len(groups) else None, books or {}, models_used)
        m = next((x for x in t["models"].values() if x), None)  # the first book's, for the CLI
        t["model"] = m
        if m:
            sign = 1 if t["side"] == "short" else -1  # short: a higher entry is better
            t["entry_vs_model"] = round(sign * (t["entry"] - m["entry_price"]), 4) if t["entry"] else None
            t["exit_vs_model"] = (round(sign * (m["exit_price"] - t["exit"]), 4)
                                  if t["exit"] and m.get("exit_price") else None)
        _split_by_book(t)
    other = [c for i, c in enumerate(charges) if i not in used and c["kind"] != "transfer"]
    transfers = [c for c in charges if c["kind"] == "transfer"]

    # ---- open positions and equity
    positions = broker.positions()
    acct = broker.account()
    unreal = round(sum(_f(p.get("unrealized_pl")) for p in positions.values()), 2)
    closed = [t for t in trips if t["closed"]]
    tot = {
        "trips": len(closed), "wins": sum(1 for t in closed if (t["net"] or 0) > 0),
        "losses": sum(1 for t in closed if (t["net"] or 0) <= 0),
        "gross": round(sum(t["gross"] for t in closed), 2),
        "fees": round(sum(t["fees"] for t in trips), 2),
        "locate": round(sum(t["locate"] for t in trips), 2),
        "borrow": round(sum(t["borrow"] for t in trips), 2),
        "other_charges": round(-sum(c["amount"] for c in other), 2),
        "transfers": round(sum(c["amount"] for c in transfers), 2),
        "unrealized": unreal,
        "gross_wins": sum(1 for t in closed if (t["gross"] or 0) > 0),
    }
    # live vs the model's own prices on the same trades, in dollars (shares x per-share gap)
    vs = [t for t in closed if t.get("model") and t.get("entry_vs_model") is not None
          and t.get("exit_vs_model") is not None]
    tot["vs_model_trips"] = len(vs)
    tot["vs_model_entry"] = round(sum(t["shares"] * t["entry_vs_model"] for t in vs), 2)
    tot["vs_model_exit"] = round(sum(t["shares"] * t["exit_vs_model"] for t in vs), 2)
    tot["net_closed"] = round(tot["gross"] - sum(t["fees"] + t["locate"] + t["borrow"]
                                                 for t in closed), 2)
    tot["net_all"] = round(tot["gross"] - tot["fees"] - tot["locate"] - tot["borrow"]
                           - tot["other_charges"] + unreal, 2)
    by_book = _book_totals(closed)
    start_eq = None
    rows = live_store.q("SELECT ts, equity FROM broker_equity ORDER BY ts")
    for r in rows:
        if _day(r["ts"]) and _day(r["ts"]) >= since:
            start_eq = float(r["equity"])
            break
    equity = float(acct["equity"])
    change = round(equity - start_eq - tot["transfers"], 2) if start_eq is not None else None
    # positions: TradeZero vs the dashboard's own fills vs the model
    dash_pos = defaultdict(int)
    for o in tz_era_orders(live_store):
        q = int(round(_f(o["filled_qty"])))
        dash_pos[o["symbol"]] += q if o["side"] == "buy" else -q
    pos_rows = []
    for sym in sorted(set(positions) | {s for s, q in dash_pos.items() if q}):
        tzq = int(float(positions[sym]["qty"])) if sym in positions else 0
        pos_rows.append({"symbol": sym, "tradezero": tzq, "dashboard_fills": dash_pos.get(sym, 0),
                         "match": tzq == dash_pos.get(sym, 0),
                         "unrealized": _f(positions[sym]["unrealized_pl"]) if sym in positions else 0.0})
    return {
        "since": since.isoformat(), "today": today.isoformat(), "account": broker.cfg.account,
        "equity": equity, "start_equity": start_eq, "equity_change": change,
        "unexplained": round(change - tot["net_all"], 2) if change is not None else None,
        "totals": tot, "by_book": by_book, "matches": matches,
        "mismatches": sum(1 for m in matches if m["status"] != "MATCH"),
        "trips": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in t.items() if k != "fills"}
                  for t in trips],
        "other_charges": [{**c, "day": c["day"].isoformat() if c["day"] else None} for c in other],
        "transfers": [{**c, "day": c["day"].isoformat() if c["day"] else None} for c in transfers],
        "positions": pos_rows, "notes": notes,
    }


def _usd(v) -> str:
    return "-" if v is None else (f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}")


def format_report(r: dict) -> str:
    t = r["totals"]
    out = [f"TradeZero {r['account']} vs the dashboard, {r['since']} to {r['today']}", ""]
    out.append("HOW WE'RE DOING")
    out.append(f"  closed round trips: {t['trips']} ({t['wins']} won, {t['losses']} lost after all "
               f"costs; {t['gross_wins']} made money before costs)")
    out.append(f"  gross trading P&L:  {_usd(t['gross'])}")
    out.append(f"  commissions + fees: {_usd(-t['fees'])}")
    out.append(f"  locate fees:        {_usd(-t['locate'])}")
    out.append(f"  overnight borrow:   {_usd(-t['borrow'])}")
    if t["other_charges"]:
        out.append(f"  other charges:      {_usd(-t['other_charges'])} (see below)")
    out.append(f"  NET, closed trips:  {_usd(t['net_closed'])}")
    out.append(f"  open positions:     {_usd(t['unrealized'])} unrealized")
    out.append(f"  NET, all in:        {_usd(t['net_all'])}")
    if t["vs_model_trips"]:
        out.append(f"  live vs the model's prices on the same {t['vs_model_trips']} trips: "
                   f"{_usd(t['vs_model_entry'] + t['vs_model_exit'])} (entries "
                   f"{_usd(t['vs_model_entry'])}, exits {_usd(t['vs_model_exit'])}; "
                   f"negative = live filled worse)")
    if r["start_equity"] is not None:
        out.append(f"  equity {_usd(r['start_equity'])} -> {_usd(r['equity'])}"
                   + (f" (transfers {_usd(t['transfers'])})" if t["transfers"] else "")
                   + f": change {_usd(r['equity_change'])}, explained {_usd(t['net_all'])}, "
                     f"unexplained {_usd(r['unexplained'])}")
    else:
        out.append(f"  equity now {_usd(r['equity'])} (no dashboard equity reading at the start "
                   f"to compare with)")
    out.append("")
    out.append(f"DO THEY MATCH: {len(r['matches']) - r['mismatches']} of {len(r['matches'])} "
               f"day/symbol/side groups match")
    for m in r["matches"]:
        out.append(f"  {m['day']} {m['symbol']:<6} {m['side']:<4}  TradeZero {m['tz_qty']:>4} @ "
                   f"{m['tz_price'] or 0:<9.4f} dashboard {m['dash_qty']:>4} @ "
                   f"{m['dash_price'] or 0:<9.4f} {m['status']}")
    for p in r["positions"]:
        out.append(f"  position {p['symbol']:<6} TradeZero {p['tradezero']:+d}, dashboard's fills "
                   f"{p['dashboard_fills']:+d}  {'MATCH' if p['match'] else 'DIFFER'}")
    out.append("")
    out.append("ROUND TRIPS (TradeZero's fills)")
    for x in r["trips"]:
        state = f"closed {x['closed']}" if x["closed"] else "OPEN"
        px = lambda v: "-" if v is None else f"{v:.4f}"
        line = (f"  {x['opened']} {x['symbol']:<6} {x['side']:<5} {x['qty']} sh  in {px(x['entry'])}"
                f"  out {px(x['exit'])}  gross {_usd(x['gross'])}  fees {_usd(-x['fees'])}"
                f"  locate {_usd(-x['locate'])}  borrow {_usd(-x['borrow'])}  net {_usd(x['net'])}  ({state})")
        out.append(line)
        m = x.get("model")
        if m:
            out.append(f"      model ({m['book']}): in {px(m['entry_price'])}, out {px(m['exit_price'])}"
                       f" ({m['exit_reason'] or m['status']}); live vs model per share: entry "
                       f"{x.get('entry_vs_model') if x.get('entry_vs_model') is not None else '-'}, exit "
                       f"{x.get('exit_vs_model') if x.get('exit_vs_model') is not None else '-'} "
                       f"(positive = live did better)")
    if r["other_charges"]:
        out.append("")
        out.append("OTHER LEDGER CHARGES (not tied to a trip)")
        for c in r["other_charges"]:
            out.append(f"  {c['day']} {c['type']}: {c['note']} {_usd(c['amount'])}")
    if r["notes"]:
        out.append("")
        out += [f"NOTE: {n}" for n in r["notes"]]
    out.append("")
    out.append("Read-only: nothing was ordered or bought.")
    return "\n".join(out)


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--since", help="first day, YYYY-MM-DD (default: the first day the dashboard "
                                    "traded at TradeZero)")
    ap.add_argument("app_dir", nargs="?", default=str(Path(__file__).resolve().parent))
    a = ap.parse_args(argv)
    app_dir = Path(a.app_dir)
    sys.path.insert(0, str(app_dir))
    import app as app_mod
    import tradezero
    from broker import BrokerError
    from store import Store
    app_mod.load_dotenv(app_dir / ".env")
    try:
        cfg = tradezero.config_from_env()
    except BrokerError as e:
        sys.exit(f"FAIL {e}")
    if cfg is None:
        sys.exit("FAIL TZ_ACCOUNT is not set in .env (see TRADEZERO.md)")
    broker = tradezero.TradeZeroBroker(cfg)
    live = Store(str(app_dir / "tranche_live.db"))
    books = {k: Store(str(app_dir / db)) for k, _m, _l, _p, db in app_mod.BOOKS}
    since = date.fromisoformat(a.since) if a.since else None
    try:
        print(format_report(reconcile(broker, live, books, since)))
    except (BrokerError, ValueError) as e:
        sys.exit(f"FAIL {e}")


if __name__ == "__main__":
    main()
