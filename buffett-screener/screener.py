#!/usr/bin/env python3
"""
Buffett quality-compounder screener (quantitative proxy layer).

Runs the T3 secondary-codifier numeric screen (Mary Buffett/Clark + Braverman)
against a universe using the Financial Modeling Prep (FMP) API, and writes a
dated markdown candidate report.

IMPORTANT — what this is and is not:
  * This automates ONLY the ~62% proxy layer. Every threshold is a secondary
    codifier's proxy for Buffett's judgment, NOT Buffett's own rule.
  * The moat / intrinsic-value / value-trap judgments that actually make the
    method work are C4 and are intentionally NOT screened here. A passing name
    is a research CANDIDATE. Confirm it by hand with research_checklist.md.
  * The ROE gate is unresolved between the two codifiers (>=12% vs >=20%); the
    screen evaluates BOTH and never picks for you.

Endpoint resilience: FMP has migrated from legacy /api/v3/ endpoints to a newer
/stable/ API, and field names vary by plan. This screener AUTO-DETECTS which
endpoint + field names actually return data for your key (logged at startup),
and reports data-coverage so a "0 candidates" run can be told apart from a
"0 data" run.

Auth: reads FMP_API_KEY from the environment. Never hard-code it.

Config via environment variables (all optional except the key):
  FMP_API_KEY   required. FMP API key.
  MAX_TICKERS   default 100. Caps calls to respect the free tier (250/day).
  DEEP          "1" to compute the exact long-term-debt/net-earnings test
                (extra calls/ticker); default off uses a labelled D/E proxy.
  OUTPUT_DIR    default ./reports
  UNIVERSE_FILE default ./universe.txt
  RUN_DATE      override the report date stamp (YYYY-MM-DD), else UTC today.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Run: pip install -r requirements.txt", file=sys.stderr)
    raise

V3 = "https://financialmodelingprep.com/api/v3"
V4 = "https://financialmodelingprep.com/api/v4"
STABLE = "https://financialmodelingprep.com/stable"
HERE = Path(__file__).resolve().parent

TREASURY_FALLBACK_10Y = 0.043  # 4.3% — labelled in the report when triggered.

# Candidate field names per metric (TTM first, then plain), tolerant to the
# v3 vs stable naming differences.
FIELDS = {
    "net_margin": ["netProfitMarginTTM", "bottomLineProfitMarginTTM",
                   "netIncomeMarginTTM", "netProfitMargin"],
    "roe": ["returnOnEquityTTM", "returnOnEquity"],
    "roic": ["returnOnInvestedCapitalTTM", "roicTTM", "returnOnCapitalEmployedTTM",
             "returnOnInvestedCapital", "roic", "returnOnCapitalEmployed"],
    "eps": ["netIncomePerShareTTM", "netIncomePerShare", "epsTTM", "eps"],
    "earnings_yield": ["earningsYieldTTM", "earningsYield"],
    "pe": ["priceToEarningsRatioTTM", "priceEarningsRatioTTM", "peRatioTTM",
           "priceEarningsRatio", "peRatio"],
    # NOTE: FMP /stable ratios use 'debtToEquityRatioTTM' (confirmed from logs);
    # legacy v3 used 'debtToEquityTTM'. Keep both plus fallbacks.
    "d_to_e": ["debtToEquityRatioTTM", "debtToEquityTTM", "debtEquityRatioTTM",
               "debtToEquityRatio", "debtToEquity"],
}


def _key() -> str:
    k = os.environ.get("FMP_API_KEY", "").strip()
    if not k:
        print("ERROR: FMP_API_KEY is not set. In GitHub Actions add it under "
              "Settings -> Secrets and variables -> Actions.", file=sys.stderr)
        sys.exit(2)
    return k


class Budget:
    """Tracks API calls so we stay inside the free-tier 250/day ceiling."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, params: dict | None = None) -> object | None:
        params = dict(params or {})
        params["apikey"] = _key()
        full = f"{url}?{urllib.parse.urlencode(params)}"
        self.calls += 1
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "buffett-screener/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - report and skip, don't crash
            print(f"  ! request failed: {url}: {exc}", file=sys.stderr)
            return None
        if isinstance(data, dict) and (data.get("Error Message") or data.get("Legacy Endpoint")):
            print(f"  ! FMP said: {url}: {data}", file=sys.stderr)
            return None
        return data


def first(obj) -> dict:
    if isinstance(obj, list) and obj:
        return obj[0] if isinstance(obj[0], dict) else {}
    if isinstance(obj, dict):
        return obj
    return {}


def _num(d: dict, k: str):
    v = (d or {}).get(k)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def pick(rec: dict, metric: str):
    for k in FIELDS[metric]:
        v = _num(rec, k)
        if v is not None:
            return v
    return None


# --- endpoint auto-detection -------------------------------------------------
# Each source is a builder: ticker -> (url, params). Tried in order; the first
# that returns a record containing any known field for `probe_metrics` wins.

RATIOS_SOURCES = [
    ("v3/ratios-ttm", lambda t: (f"{V3}/ratios-ttm/{t}", {})),
    ("stable/ratios-ttm", lambda t: (f"{STABLE}/ratios-ttm", {"symbol": t})),
]
KEYMETRICS_SOURCES = [
    ("v3/key-metrics-ttm", lambda t: (f"{V3}/key-metrics-ttm/{t}", {})),
    ("stable/key-metrics-ttm", lambda t: (f"{STABLE}/key-metrics-ttm", {"symbol": t})),
]


def resolve_source(api: Budget, sources: list, sample: str, probe_metrics: list, label: str):
    """Return (name, builder) for the first source that yields usable data, else None."""
    for name, build in sources:
        url, params = build(sample)
        rec = first(api.get(url, params))
        if rec and any(pick(rec, m) is not None for m in probe_metrics):
            keys = sorted(rec.keys())
            print(f"[resolve] {label}: using '{name}'. sample fields: {keys[:25]}"
                  f"{'…' if len(keys) > 25 else ''}")
            return name, build
    print(f"[resolve] {label}: NO working endpoint (tried {[n for n, _ in sources]}) — "
          f"key/plan likely lacks this data.")
    return None


def ten_year_yield(api: Budget) -> tuple[float, bool]:
    today = datetime.now(timezone.utc).date()
    frm = today.replace(day=1).isoformat()
    for url, params, field in [
        (f"{V4}/treasury", {"from": frm, "to": today.isoformat()}, "year10"),
        (f"{STABLE}/treasury-rates", {"from": frm, "to": today.isoformat()}, "year10"),
    ]:
        data = api.get(url, params)
        rec = None
        if isinstance(data, list) and data:
            rec = sorted(data, key=lambda r: r.get("date", ""))[-1]
        elif isinstance(data, dict):
            rec = data
        if rec:
            for f in (field, "10year", "tenYear", "month10"):
                y = _num(rec, f)
                if y is not None:
                    return (y / 100.0 if y > 1 else y), True
    return TREASURY_FALLBACK_10Y, False


def score(ticker: str, values: dict, sig: dict) -> dict:
    """Pure signal-scoring logic — no network, no I/O. Unit-tested offline.

    A missing (None) core input is treated conservatively as a FAIL, never a pass.
    """
    s = sig["signals"]
    net_margin = values.get("net_margin")
    roe = values.get("roe")
    roic = values.get("roic")
    eps = values.get("eps")
    lev_pass = values.get("lev_pass")

    checks = {
        "net_margin>15%": (net_margin is not None and net_margin > s["net_margin"]["value"]),
        "roic>=12%": (roic is not None and roic >= s["return_on_capital"]["value"]),
        "leverage_ok": lev_pass,
        "eps>0": (eps is not None and eps > 0),
    }
    tiers = s["roe_gate"]["tiers"]  # [0.12, 0.20]
    roe12 = roe is not None and roe >= tiers[0]
    roe20 = roe is not None and roe >= tiers[1]
    passes_core_ex_roe = all(v is True for v in checks.values())
    return {
        "ticker": ticker,
        "net_margin": net_margin, "roe": roe, "roic": roic, "eps": eps,
        "earnings_yield": values.get("earnings_yield"), "pe": values.get("pe"),
        "leverage_desc": values.get("lev_desc", ""),
        "checks": checks,
        "has_data": any(values.get(k) is not None for k in ("net_margin", "roe", "roic")),
        "passes_core_ex_roe": passes_core_ex_roe,
        "pass_at_roe12": passes_core_ex_roe and roe12,
        "pass_at_roe20": passes_core_ex_roe and roe20,
    }


def leverage_test(d_to_e, ltd_ni, sig: dict, deep: bool) -> tuple:
    """Resolve the conservative-leverage signal. Returns (lev_pass, lev_desc).

    deep=True uses the exact long-term-debt / net-earnings ratio via ltd_ni=(ltd, ni);
    otherwise a labelled debt/equity proxy from d_to_e is used. Pure/testable.
    """
    lev = sig["signals"]["conservative_leverage"]
    if deep:
        ltd, ni = (ltd_ni or (None, None))
        if ltd is not None and ni and ni > 0:
            ratio = ltd / ni
            return ratio < float(lev["exact_value"]), f"LTD/NI={ratio:.1f} (<5 exact)"
        return None, "LTD/NI=n/a"
    if d_to_e is not None:
        return d_to_e < float(lev["approx_value"]), f"D/E={d_to_e:.2f} (<{lev['approx_value']} proxy)"
    return None, "D/E=n/a"


def evaluate(ticker: str, api: Budget, sig: dict, deep: bool,
             ratios_build, metrics_build) -> dict:
    ratios = first(api.get(*ratios_build(ticker))) if ratios_build else {}
    metrics = first(api.get(*metrics_build(ticker))) if metrics_build else {}

    ltd_ni = None
    if deep:
        inc = first(api.get(f"{V3}/income-statement/{ticker}", {"period": "annual", "limit": 1}))
        bal = first(api.get(f"{V3}/balance-sheet-statement/{ticker}", {"period": "annual", "limit": 1}))
        try:
            ltd_ni = (float(bal.get("longTermDebt")), float(inc.get("netIncome")))
        except (TypeError, ValueError, AttributeError):
            ltd_ni = None

    d_to_e = pick(metrics, "d_to_e")
    if d_to_e is None:
        d_to_e = pick(ratios, "d_to_e")
    lev_pass, lev_desc = leverage_test(d_to_e, ltd_ni, sig, deep)

    values = {
        "net_margin": pick(ratios, "net_margin") if pick(ratios, "net_margin") is not None else pick(metrics, "net_margin"),
        # ROE lives in key-metrics on the /stable API (not ratios); fall back either way.
        "roe": pick(metrics, "roe") if pick(metrics, "roe") is not None else pick(ratios, "roe"),
        "roic": pick(metrics, "roic") if pick(metrics, "roic") is not None else pick(ratios, "roic"),
        "eps": pick(metrics, "eps") if pick(metrics, "eps") is not None else pick(ratios, "eps"),
        "earnings_yield": pick(metrics, "earnings_yield"),
        "pe": pick(ratios, "pe") if pick(ratios, "pe") is not None else pick(metrics, "pe"),
        "lev_pass": lev_pass, "lev_desc": lev_desc,
    }
    return score(ticker, values, sig)


def pct(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "n/a"


def load_signals() -> dict:
    with open(HERE / "signals.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_universe() -> list[str]:
    path = Path(os.environ.get("UNIVERSE_FILE", HERE / "universe.txt"))
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            out.append(line)
    return out


def build_report(rows, y10, y10_live, universe_n, coverage, calls, deep, run_date,
                 endpoints) -> str:
    cand12 = sorted([r for r in rows if r["pass_at_roe12"]], key=lambda r: (r["roic"] or 0), reverse=True)
    cand20 = sorted([r for r in rows if r["pass_at_roe20"]], key=lambda r: (r["roic"] or 0), reverse=True)
    L = []
    L.append(f"# Buffett quality-compounder scan — {run_date}")
    L.append("")
    L.append("> **These are RESEARCH CANDIDATES, not buys.** Every threshold below is a")
    L.append("> secondary-codifier PROXY (Mary Buffett/Clark, Braverman) for Buffett's own")
    L.append("> C4 moat & intrinsic-value judgment — which is deliberately NOT screened here.")
    L.append("> Confirm each name by hand with `research_checklist.md` before it is investable.")
    L.append("")
    if coverage == 0:
        L.append("> ⚠️ **DATA WARNING: 0 of the screened tickers returned usable fundamentals.**")
        L.append("> This is a data-access problem (endpoint/plan), NOT a real empty screen. See")
        L.append("> the run logs' `[resolve]` lines for which FMP endpoints failed.")
        L.append("")
    tre = f"{y10*100:.2f}%" + ("" if y10_live else "  *(FALLBACK — live 10y unavailable; rebase)*")
    L.append(f"- Universe screened: **{universe_n}**  |  **data coverage: {coverage}/{universe_n}**  "
             f"|  API calls: **{calls}**  |  leverage: **{'exact' if deep else 'D/E proxy'}**")
    L.append(f"- Endpoints used: {endpoints}")
    L.append(f"- 10y Treasury (earnings-yield benchmark): **{tre}**")
    L.append(f"- Candidates at ROE ≥ 12% (Mary Buffett): **{len(cand12)}**  |  "
             f"at ROE ≥ 20% (Braverman): **{len(cand20)}**")
    L.append("")

    def table(cands, title):
        L.append(f"## {title} — {len(cands)} name(s)")
        if not cands:
            L.append("_None cleared every core signal today._")
            L.append("")
            return
        L.append("| Ticker | Net margin | ROE | ROIC | Earn. yield vs 10y | P/E | Leverage |")
        L.append("|---|---|---|---|---|---|---|")
        for r in cands:
            ey = r["earnings_yield"]
            ey_cell = "n/a"
            if isinstance(ey, (int, float)):
                ey_cell = f"{ey*100:.1f}% {'↑' if ey >= y10 else '↓ (rich)'}"
            pe = f"{r['pe']:.0f}" if isinstance(r["pe"], (int, float)) else "n/a"
            pe_flag = " ⚠️≥40" if isinstance(r["pe"], (int, float)) and r["pe"] >= 40 else ""
            L.append(f"| {r['ticker']} | {pct(r['net_margin'])} | {pct(r['roe'])} | "
                     f"{pct(r['roic'])} | {ey_cell} | {pe}{pe_flag} | {r['leverage_desc']} |")
        L.append("")

    table(cand20, "Candidates — strict tier (ROE ≥ 20%, Braverman)")
    table(cand12, "Candidates — base tier (ROE ≥ 12%, Mary Buffett)")
    L.append("## Next step for every candidate")
    L.append("Open `research_checklist.md` and run the five gates in order: circle of")
    L.append("competence → moat durability → one-time-vs-terminal problem → management →")
    L.append("valuation/margin of safety. A 'no' on any gate kills the name regardless of")
    L.append("how clean the numbers are — the numbers are only proxies for gates 2–3.")
    L.append("")
    L.append("_Signals & provenance: see `signals.yaml`. Screen automates the proxy layer only._")
    return "\n".join(L)


def main() -> int:
    api = Budget()
    sig = load_signals()
    universe = load_universe()[: int(os.environ.get("MAX_TICKERS", "100"))]
    deep = os.environ.get("DEEP", "").strip() in ("1", "true", "yes")
    run_date = os.environ.get("RUN_DATE") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(os.environ.get("OUTPUT_DIR", HERE / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    if not universe:
        print("Empty universe.", file=sys.stderr)
        return 1

    sample = universe[0]
    print(f"Resolving FMP endpoints against sample ticker {sample} …")
    ratios_src = resolve_source(api, RATIOS_SOURCES, sample, ["net_margin", "roe"], "ratios")
    metrics_src = resolve_source(api, KEYMETRICS_SOURCES, sample, ["roic", "eps", "earnings_yield"], "key-metrics")
    ratios_build = ratios_src[1] if ratios_src else None
    metrics_build = metrics_src[1] if metrics_src else None
    endpoints = f"ratios={ratios_src[0] if ratios_src else 'NONE'}, " \
                f"key-metrics={metrics_src[0] if metrics_src else 'NONE'}"

    y10, y10_live = ten_year_yield(api)
    print(f"Screening {len(universe)} tickers (deep={deep}); {endpoints}")

    rows, coverage = [], 0
    for i, t in enumerate(universe, 1):
        r = evaluate(t, api, sig, deep, ratios_build, metrics_build)
        rows.append(r)
        if i == 1:  # per-metric visibility on the first name to catch field gaps
            print(f"[sample {t}] net_margin={r['net_margin']} roe={r['roe']} "
                  f"roic={r['roic']} eps={r['eps']} pe={r['pe']} lev='{r['leverage_desc']}'")
        if r["has_data"]:
            coverage += 1
        if i % 20 == 0:
            print(f"  {i}/{len(universe)} done, coverage {coverage}, {api.calls} calls")
        time.sleep(0.15)

    report = build_report(rows, y10, y10_live, len(universe), coverage, api.calls,
                          deep, run_date, endpoints)
    (out_dir / f"buffett_scan_{run_date}.md").write_text(report, encoding="utf-8")
    (out_dir / "latest.md").write_text(report, encoding="utf-8")

    n12 = sum(1 for r in rows if r["pass_at_roe12"])
    n20 = sum(1 for r in rows if r["pass_at_roe20"])
    print(f"\nDone. coverage {coverage}/{len(universe)}. "
          f"{n12} candidate(s) at ROE>=12%, {n20} at ROE>=20%. {api.calls} API calls.")
    if coverage == 0:
        print("WARNING: zero data coverage — see [resolve] lines above; likely endpoint/plan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
