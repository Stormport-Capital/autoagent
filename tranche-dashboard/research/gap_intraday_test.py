"""Gap-down breakdown short, re-tested on 5-minute bars (research only; never trades).

Re-runs the daily-bar study "Equity Gap-Down Intraday Outcomes 2021-2026" with
the two things daily bars cannot give: a relative-volume reading taken at 9:35
(premarket plus the first regular bar, against the 20-day average) and a real
intraday stop, checked bar by bar. Stocks come from that study's CSV; ETFs are
found here from FMP's daily history. Nothing is sent to a broker and no
dashboard database is opened.

    python -I gap_intraday_test.py --csv gapdown10.csv --env /path/to/.env --out gap_out
    python -I gap_intraday_test.py --csv gapdown10.csv --env ... --out gap_out --etfs
    python -I gap_intraday_test.py --csv gapdown10.csv --env ... --out gap_out --limit 20   # smoke run

Reads FMP_API_KEY from --env (the dashboard's .env) or the environment; the key
is never printed. Every FMP answer is cached under <out>/cache, so a rerun with
different rules costs no requests. Writes <out>/trades.csv, <out>/etf_events.csv
and <out>/summary.txt.

Rules simulated per event day (parameters below are flags):
  * universe: stocks with open in [--price-min, --price-max] and 20-day average
    volume >= --adv-min; ETFs with open >= --etf-price-min, average volume
    >= --etf-adv-shares and >= --etf-adv-dollars a day, gap <= --gap;
  * pace at 9:35 = (premarket volume 4:00-9:30 + the 9:30-9:35 bar) / (0.08 x
    20-day average daily volume). The 0.08 is the study's assumption; the
    actual share of the day's volume done by 9:35 is reported so it can be
    checked;
  * entry: a sell stop-limit at open x (1 - --breakdown), limit open x
    (1 - --limit-guard), working from 9:35 until --entry-deadline. It fills on
    the first bar whose low touches the stop: at the stop if the bar opened
    above it, at the bar's open if that is between limit and stop, and at the
    limit if the bar opened below the limit and traded back up through it;
  * stop: cover at entry x (1 + --stop) when a later bar's high reaches it
    (at the bar's open if it gapped through); the entry bar itself is not
    judged, a bar with both the entry and a stop touch counts as stopped out
    ("conservative"). A second column judges the stop on bar CLOSES above the
    level, which is how the dashboard's live stop works;
  * otherwise flat at the close of the 15:50-15:55 bar, as the dashboard is;
  * slippage --slip-bps against the trade on each side.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics as st
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:  # the droplet's venv has it; a bare python may not
    requests = None

FMP = "https://financialmodelingprep.com/stable"
LEVERAGED = re.compile(r"\b(2x|3x|-1x|1\.5x|bull|bear|ultra(?:short|pro)?|daily|leveraged|inverse)\b", re.I)


# ----------------------------------------------------------------- fetching
class Fetcher:
    """FMP GET with a per-minute rate limit, retries and a disk cache."""

    def __init__(self, key: str, cache: Path, per_minute: float = 250.0):
        self.key, self.cache = key, cache
        self.gap = 60.0 / per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.calls = 0
        self.errors = 0
        cache.mkdir(parents=True, exist_ok=True)

    def _wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.monotonic()
            self.next_at = now + self.gap

    def get(self, name: str, path: str, params: dict):
        """JSON from FMP, cached under `name`. None when FMP has nothing."""
        f = self.cache / (name + ".json")
        if f.exists():
            return json.loads(f.read_text())
        if requests is None:
            raise SystemExit("the requests package is needed to fetch (pip install requests)")
        body = None
        for attempt in range(6):
            self._wait()
            try:
                r = requests.get(f"{FMP}/{path}", params={**params, "apikey": self.key}, timeout=30)
                self.calls += 1
            except requests.RequestException:
                self.errors += 1
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                self.errors += 1
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code != 200:
                self.errors += 1
                raise RuntimeError(f"FMP HTTP {r.status_code} for {path} {params.get('symbol')}: "
                                   f"{r.text[:120].replace(self.key, '***')}")
            body = r.json()
            break
        else:
            raise RuntimeError(f"FMP kept failing for {path} {params.get('symbol')}")
        f.write_text(json.dumps(body))
        return body


def five_min(fx: Fetcher, symbol: str, day: str) -> list[dict]:
    rows = fx.get(f"5min_{symbol}_{day}", "historical-chart/5min",
                  {"symbol": symbol, "from": day, "to": day, "extended": "true"})
    return sorted((r for r in rows or [] if isinstance(r, dict) and r.get("date", "")[:10] == day),
                  key=lambda r: r["date"])


def daily(fx: Fetcher, symbol: str, start: str, end: str) -> list[dict]:
    rows = fx.get(f"eod_{symbol}", "historical-price-eod/full",
                  {"symbol": symbol, "from": start, "to": end})
    return sorted((r for r in rows or [] if isinstance(r, dict) and r.get("date")), key=lambda r: r["date"])


# -------------------------------------------------------------- pure rules
def bar_time(r: dict) -> str:
    return r["date"][11:16]


def split_session(bars: list[dict]) -> tuple[list[dict], list[dict]]:
    """(premarket 4:00-9:30 bars, regular 9:30-16:00 bars), FMP stamps = bar start."""
    pre = [b for b in bars if "04:00" <= bar_time(b) < "09:30"]
    reg = [b for b in bars if "09:30" <= bar_time(b) < "16:00"]
    return pre, reg


def adv20(eod: list[dict], day: str) -> float | None:
    prior = [float(r.get("volume") or 0) for r in eod if r["date"] < day][-20:]
    return sum(prior) / 20 if len(prior) == 20 else None


def pace_935(pre: list[dict], reg: list[dict], adv: float | None, frac: float = 0.08) -> dict:
    v_pre = sum(float(b.get("volume") or 0) for b in pre)
    first = next((b for b in reg if bar_time(b) == "09:30"), None)
    v_930 = float(first.get("volume") or 0) if first else 0.0
    v_935 = v_pre + v_930
    return {"vol_pm": v_pre, "vol_930": v_930, "vol_935": v_935,
            "pace": (v_935 / (frac * adv)) if adv else None}


def simulate(reg: list[dict], open_px: float, *, breakdown=0.02, limit_guard=0.025, stop=0.06,
             deadline="11:30", slip_bps=10.0) -> dict:
    """One event day on its regular-hours 5-minute bars. Returns the trade, or
    why there was none. Returns are % on the short, slippage included."""
    stop_px, limit_px = open_px * (1 - breakdown), open_px * (1 - limit_guard)
    slip = slip_bps / 1e4
    entry = None
    for i, b in enumerate(reg):
        t = bar_time(b)
        if t < "09:35":
            continue
        if t >= deadline:
            break
        o, h, lo = float(b["open"]), float(b["high"]), float(b["low"])
        if lo > stop_px:
            continue
        if o >= stop_px:
            px = stop_px
        elif o >= limit_px:
            px = o
        elif h >= limit_px:
            px = limit_px
        else:
            continue  # opened below the limit and never traded back up to it
        entry = (i, t, px * (1 - slip))
        break
    if entry is None:
        return {"entry_time": None, "reason": "no breakdown fill by the deadline"}
    i, t_in, fill = entry
    cover = fill * (1 + stop)
    flat_bar = next((b for b in reg if bar_time(b) == "15:50"), reg[-1])
    out = {"entry_time": t_in, "entry_px": round(fill, 4), "stop_px": round(cover, 4),
           "close_1600": float(reg[-1]["close"])}

    def done(when, px, why):
        px = px * (1 + slip)
        return {"exit_time": when, "exit_px": round(px, 4), "exit_reason": why,
                "ret_pct": round((fill - px) / fill * 100, 3)}

    # conservative stop: the high of the entry bar or any later bar touches it
    touch = None
    for b in reg[i:]:
        if bar_time(b) > "15:50":
            break
        if float(b["high"]) >= cover:
            px = max(float(b["open"]), cover) if b is not reg[i] else cover
            touch = done(bar_time(b), px, "stop touched" + (" (entry bar)" if b is reg[i] else ""))
            break
    # dashboard-style stop: a later bar CLOSES above it
    closed = None
    for b in reg[i + 1:]:
        if bar_time(b) > "15:50":
            break
        if float(b["close"]) > cover:
            closed = done(bar_time(b), float(b["close"]), "stop on a 5-min close")
            break
    flat = done("15:55", float(flat_bar["close"]), "flat at 15:55")
    out.update(touch or flat)
    out["ret_pct_close_stop"] = (closed or flat)["ret_pct"]
    out["ret_pct_no_stop"] = flat["ret_pct"]
    return out


# ------------------------------------------------------------------ events
def stock_events(csv_path: Path, lo: float, hi: float) -> list[dict]:
    out = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                o, low, high = float(r["open"]), float(r["low"]), float(r["high"])
            except (TypeError, ValueError):
                continue
            if r.get("flag") or low > o or high < o or not lo <= o <= hi:
                continue
            out.append({"symbol": r["symbol"], "date": r["trade_date"], "kind": "stock",
                        "csv_open": o, "csv_prev_close": float(r["prev_close"]),
                        "csv_vol_multiple": float(r["vol_multiple"]) if r.get("vol_multiple") else None})
    return out


def etf_events(fx: Fetcher, start: str, end: str, gap: float, price_min: float, adv_shares: float,
               adv_dollars: float, log) -> list[dict]:
    lst = fx.get("etf_list", "etf-list", {}) or []
    us = {"NYSE", "NASDAQ", "AMEX", "BATS", "ARCA", "NYSE ARCA", "CBOE", "NYSEARCA", "NYSE MKT"}

    def listed_here(r):  # the list may carry an exchange field; keep US venues when it does
        ex = str(r.get("exchangeShortName") or r.get("exchange") or "").upper()
        return not ex or ex in us
    syms = sorted({r["symbol"] for r in lst if isinstance(r, dict) and r.get("symbol")
                   and re.fullmatch(r"[A-Z]{1,5}", r["symbol"]) and listed_here(r)})
    names = {r["symbol"]: r.get("name") or "" for r in lst if isinstance(r, dict)}
    log(f"ETF list: {len(syms)} symbols; fetching daily history for each")
    out = []
    for n, s in enumerate(syms, 1):
        if n % 250 == 0:
            log(f"  ETF history {n}/{len(syms)}, {fx.calls} requests so far")
        try:
            eod = daily(fx, s, start, end)
        except RuntimeError as e:
            log(f"  {s}: {e}")
            continue
        for k in range(21, len(eod)):
            r, prev = eod[k], eod[k - 1]
            try:
                o, pc = float(r["open"]), float(prev["close"])
            except (TypeError, ValueError):
                continue
            if not pc or o < price_min or (o - pc) / pc > gap or (o - pc) / pc < -0.6:
                continue
            adv = adv20(eod, r["date"])
            if not adv or adv < adv_shares or adv * pc < adv_dollars:
                continue
            out.append({"symbol": s, "date": r["date"], "kind": "lev_etf" if LEVERAGED.search(names[s]) else "etf",
                        "name": names[s], "csv_open": o, "csv_prev_close": pc, "csv_vol_multiple": None})
    log(f"ETF events found: {len(out)} ({sum(1 for e in out if e['kind'] == 'lev_etf')} leveraged)")
    return out


# ----------------------------------------------------------------- summary
def tstat(v):
    n = len(v)
    if n < 2:
        return float("nan")
    s = st.pstdev(v)
    return st.mean(v) / (s / math.sqrt(n)) if s else float("nan")


def line(label: str, rets: list[float], days: dict | None = None) -> str:
    if not rets:
        return f"{label:<44} n=0"
    s = (f"{label:<44} n={len(rets):<5} mean {st.mean(rets):+6.2f}%  median {st.median(rets):+6.2f}%  "
         f"win {sum(1 for x in rets if x > 0) / len(rets):5.1%}  t {tstat(rets):+5.1f}")
    if days:
        dm = [st.mean(v) for v in days.values()]
        s += f"  | days {len(dm)} mean {st.mean(dm):+5.2f}% day-win {sum(1 for x in dm if x > 0) / len(dm):4.0%} t {tstat(dm):+4.1f}"
    return s


def bucket_pace(p):
    if p is None:
        return "pace unknown"
    return "pace <1" if p < 1 else "pace 1-2" if p < 2 else "pace 2-4" if p < 4 else "pace >=4"


def summarize(trades: list[dict], macro_days: set, args) -> str:
    out = [f"Gap-down breakdown short on 5-minute bars: entry stop -{args.breakdown:.1%} below the open, "
           f"limit -{args.limit_guard:.1%}, stop +{args.stop:.0%}, flat 15:55, slippage {args.slip_bps:.0f} bps a side",
           ""]
    skips = defaultdict(int)
    for t in trades:
        if t.get("skip"):
            skips[t["skip"]] += 1
    out[-1] = (f"{len(trades)} events; {sum(1 for t in trades if t.get('entry_time'))} filled; skipped: "
               + (", ".join(f"{k} {v}" for k, v in sorted(skips.items())) or "none"))
    filled = [t for t in trades if t.get("entry_time")]

    def days(ts, key):
        d = defaultdict(list)
        for t in ts:
            d[t["date"]].append(t[key])
        return d

    for kind in ("stock", "etf", "lev_etf"):
        ts = [t for t in filled if t["kind"] == kind]
        if not ts:
            continue
        out += ["", f"=== {kind} ==="]
        for key, label in (("ret_pct", "stop touched intrabar (conservative)"),
                           ("ret_pct_close_stop", "stop on 5-min closes (dashboard rule)"),
                           ("ret_pct_no_stop", "no stop")):
            out.append(line(f"all, {label}", [t[key] for t in ts], days(ts, key)))
        out.append("")
        for b in ("pace <1", "pace 1-2", "pace 2-4", "pace >=4", "pace unknown"):
            sub = [t for t in ts if bucket_pace(t.get("pace")) == b]
            if sub:
                out.append(line(f"{b}, intrabar stop", [t["ret_pct"] for t in sub], days(sub, "ret_pct")))
                out.append(line(f"{b}, close stop", [t["ret_pct_close_stop"] for t in sub]))
        out.append("")
        for b in ("eod x<3", "eod x3-5", "eod x5-8", "eod x>=8"):
            sub = [t for t in ts if t.get("eod_mult") is not None and
                   b == ("eod x<3" if t["eod_mult"] < 3 else "eod x3-5" if t["eod_mult"] < 5
                         else "eod x5-8" if t["eod_mult"] < 8 else "eod x>=8")]
            if sub:
                out.append(line(f"{b} (look-ahead, for comparison), intrabar stop", [t["ret_pct"] for t in sub]))
        out.append("")
        nm = [t for t in ts if t["date"] not in macro_days]
        out.append(line("excluding macro days (>=35 stock gaps), intrabar stop", [t["ret_pct"] for t in nm], days(nm, "ret_pct")))
        top = [t for t in nm if (t.get("pace") or 0) >= 4]
        out.append(line("  and pace >=4", [t["ret_pct"] for t in top], days(top, "ret_pct")))
        out.append("")
        byy = defaultdict(list)
        for t in ts:
            byy[t["date"][:4]].append(t["ret_pct"])
        for y in sorted(byy):
            out.append(line(f"{y}, intrabar stop", byy[y]))
        pairs = [(t["pace"], t["eod_mult"]) for t in ts if t.get("pace") is not None and t.get("eod_mult") is not None]
        if len(pairs) > 10:
            xs, ys = zip(*pairs)
            mx, my = st.mean(xs), st.mean(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
            fr = [t["frac_935"] for t in ts if t.get("frac_935") is not None]
            out.append(f"9:35 pace vs end-of-day multiple: r = {cov / den if den else float('nan'):.2f} "
                       f"(n={len(pairs)}); share of the day's volume done by 9:35: median "
                       f"{st.median(fr):.1%}, mean {st.mean(fr):.1%} (the pace formula assumes 8%)")
        worst = sorted(ts, key=lambda t: t["ret_pct"])[:5]
        out.append("worst 5 (intrabar stop): " + ", ".join(f"{t['symbol']} {t['date']} {t['ret_pct']:+.1f}%" for t in worst))
        stops = sum(1 for t in ts if t["exit_reason"].startswith("stop"))
        out.append(f"stopped out: {stops}/{len(ts)} = {stops / len(ts):.1%}; "
                   f"entries before 10:00: {sum(1 for t in ts if t['entry_time'] < '10:00') / len(ts):.0%}")
    return "\n".join(out)


# -------------------------------------------------------------------- main
def run_event(fx: Fetcher, ev: dict, args, eod_start: str, eod_end: str) -> dict:
    t = dict(ev)
    try:
        bars = five_min(fx, ev["symbol"], ev["date"])
        eod = daily(fx, ev["symbol"], eod_start, eod_end)
    except RuntimeError as e:
        t["skip"] = "fetch failed"
        t["note"] = str(e)[:100]
        return t
    pre, reg = split_session(bars)
    first = next((b for b in reg if bar_time(b) == "09:30"), None)
    if first is None or len(reg) < 60:
        t["skip"] = "no intraday bars"
        return t
    open_px = float(first["open"])
    t["open"] = open_px
    if abs(open_px - ev["csv_open"]) / ev["csv_open"] > 0.03:
        t["skip"] = "open disagrees with daily data (split or bad print)"
        t["note"] = f"5-min open {open_px} vs daily {ev['csv_open']}"
        return t
    adv = adv20(eod, ev["date"])
    if adv is None:
        t["skip"] = "no 20-day volume history"
        return t
    if ev["kind"] == "stock" and adv < args.adv_min:
        t["skip"] = "average volume below the floor"
        return t
    t["adv20"] = round(adv)
    t.update({k: round(v) if isinstance(v, float) and k != "pace" else v
              for k, v in pace_935(pre, reg, adv).items()})
    t["pace"] = round(t["pace"], 2) if t["pace"] is not None else None
    today = next((r for r in eod if r["date"] == ev["date"]), None)
    day_vol = float(today.get("volume") or 0) if today else sum(float(b.get("volume") or 0) for b in reg)
    t["eod_mult"] = round(day_vol / adv, 2) if adv else None
    t["frac_935"] = round(t["vol_935"] / day_vol, 4) if day_vol else None
    t["gap_pct"] = round((open_px - ev["csv_prev_close"]) / ev["csv_prev_close"] * 100, 2)
    t.update(simulate(reg, open_px, breakdown=args.breakdown, limit_guard=args.limit_guard,
                      stop=args.stop, deadline=args.entry_deadline, slip_bps=args.slip_bps))
    return t


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="the study's gap-down CSV (stocks)")
    ap.add_argument("--env", help=".env file holding FMP_API_KEY (else the environment)")
    ap.add_argument("--out", default="gap_out")
    ap.add_argument("--etfs", action="store_true", help="also find and test ETF gap-downs")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stock events (smoke run)")
    ap.add_argument("--rate", type=float, default=250, help="FMP requests per minute")
    ap.add_argument("--price-min", type=float, default=15)
    ap.add_argument("--price-max", type=float, default=500)
    ap.add_argument("--adv-min", type=float, default=250_000, help="stocks: 20-day average shares")
    ap.add_argument("--etf-price-min", type=float, default=10)
    ap.add_argument("--etf-adv-shares", type=float, default=1_000_000)
    ap.add_argument("--etf-adv-dollars", type=float, default=20_000_000)
    ap.add_argument("--gap", type=float, default=-0.10, help="ETFs: open vs prior close at most this")
    ap.add_argument("--breakdown", type=float, default=0.02)
    ap.add_argument("--limit-guard", type=float, default=0.025)
    ap.add_argument("--stop", type=float, default=0.06)
    ap.add_argument("--entry-deadline", default="11:30")
    ap.add_argument("--slip-bps", type=float, default=10)
    ap.add_argument("--start", default="2021-09-01")
    ap.add_argument("--end", default=date.today().isoformat())
    args = ap.parse_args(argv)

    key = os.environ.get("FMP_API_KEY", "")
    if args.env:
        for ln in Path(args.env).read_text(encoding="utf-8-sig").splitlines():
            if ln.strip().startswith("FMP_API_KEY="):
                key = ln.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("no FMP key: pass --env /path/to/.env or set FMP_API_KEY")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "run.log", "a")

    def log(msg):
        s = f"{datetime.now():%H:%M:%S} {msg}"
        print(s, flush=True)
        logf.write(s + "\n")
        logf.flush()

    fx = Fetcher(key, out / "cache", args.rate)
    events = stock_events(Path(args.csv), args.price_min, args.price_max)
    log(f"stock events in [{args.price_min:g}, {args.price_max:g}]: {len(events)}")
    if args.limit:
        events = events[:args.limit]
    # macro days from the whole CSV (all price tiers), before any filtering
    counts = defaultdict(int)
    with open(args.csv, newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("flag"):
                counts[r["trade_date"]] += 1
    macro = {d for d, c in counts.items() if c >= 35}
    if args.etfs:
        events += etf_events(fx, args.start, args.end, args.gap, args.etf_price_min, args.etf_adv_shares,
                             args.etf_adv_dollars, log)
        with open(out / "etf_events.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["symbol", "date", "kind", "name", "csv_open", "csv_prev_close"])
            w.writeheader()
            w.writerows({k: e.get(k) for k in w.fieldnames} for e in events if e["kind"] != "stock")

    trades = []
    for n, ev in enumerate(events, 1):
        trades.append(run_event(fx, ev, args, args.start, args.end))
        if n % 100 == 0:
            log(f"  {n}/{len(events)} events, {fx.calls} requests, {fx.errors} retries")
    log(f"done: {len(trades)} events, {fx.calls} requests, {fx.errors} retries")

    cols = ["symbol", "date", "kind", "name", "gap_pct", "open", "adv20", "vol_pm", "vol_930", "vol_935", "pace",
            "eod_mult", "frac_935", "entry_time", "entry_px", "stop_px", "exit_time", "exit_px", "exit_reason",
            "ret_pct", "ret_pct_close_stop", "ret_pct_no_stop", "close_1600", "skip", "note", "csv_vol_multiple"]
    with open(out / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    text = summarize(trades, macro, args)
    (out / "summary.txt").write_text(text + "\n")
    print("\n" + text)


if __name__ == "__main__":
    main()
