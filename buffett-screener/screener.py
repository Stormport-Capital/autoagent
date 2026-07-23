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

Auth: reads FMP_API_KEY from the environment. Never hard-code it.

Config via environment variables (all optional except the key):
  FMP_API_KEY   required. FMP API key.
  MAX_TICKERS   default 100. Caps calls to respect the free tier (250/day).
  DEEP          "1" to compute the exact long-term-debt/net-earnings test
                (4 calls/ticker); default off uses a labelled debt/equity proxy
                (2 calls/ticker).
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

BASE = "https://financialmodelingprep.com/api"
HERE = Path(__file__).resolve().parent

# ---- Treasury fallback: used only if the live 10y fetch is unavailable. -------
# This is a date-bound assumption; the report labels it clearly when used.
TREASURY_FALLBACK_10Y = 0.043  # 4.3% — REBASE THIS if the fallback is triggered.


def _key() -> str:
    k = os.environ.get("FMP_API_KEY", "").strip()
    if not k:
        print(
            "ERROR: FMP_API_KEY is not set. In GitHub Actions add it under "
            "Settings -> Secrets and variables -> Actions. Locally: "
            "export FMP_API_KEY=...",
            file=sys.stderr,
        )
        sys.exit(2)
    return k


class Budget:
    """Tracks API calls so we stay inside the free-tier 250/day ceiling."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, path: str, params: dict | None = None) -> object | None:
        params = dict(params or {})
        params["apikey"] = _key()
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        self.calls += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "buffett-screener/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - report and skip, don't crash the run
            print(f"  ! request failed for {path}: {exc}", file=sys.stderr)
            return None
        if isinstance(data, dict) and data.get("Error Message"):
            print(f"  ! FMP error for {path}: {data['Error Message']}", file=sys.stderr)
            return None
        return data


def first(obj) -> dict:
    if isinstance(obj, list) and obj:
        return obj[0]
    if isinstance(obj, dict):
        return obj
    return {}


def load_signals() -> dict:
    with open(HERE / "signals.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_universe() -> list[str]:
    path = Path(os.environ.get("UNIVERSE_FILE", HERE / "universe.txt"))
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            tickers.append(line)
    return tickers


def ten_year_yield(api: Budget) -> tuple[float, bool]:
    """Return (yield_as_fraction, is_live). Falls back to a labelled constant."""
    today = datetime.now(timezone.utc).date()
    data = api.get(
        "v4/treasury",
        {"from": (today.replace(day=1)).isoformat(), "to": today.isoformat()},
    )
    if isinstance(data, list) and data:
        try:
            latest = sorted(data, key=lambda r: r.get("date", ""))[-1]
            y = float(latest["year10"])
            # FMP reports treasury as a percent number (e.g. 4.3), normalise.
            return (y / 100.0 if y > 1 else y), True
        except (KeyError, ValueError, TypeError):
            pass
    return TREASURY_FALLBACK_10Y, False


def evaluate(ticker: str, api: Budget, sig: dict, deep: bool) -> dict | None:
    ratios = first(api.get(f"v3/ratios-ttm/{ticker}"))
    metrics = first(api.get(f"v3/key-metrics-ttm/{ticker}"))
    if not ratios and not metrics:
        return None

    def num(d: dict, k: str):
        v = d.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    net_margin = num(ratios, "netProfitMarginTTM")
    roe = num(ratios, "returnOnEquityTTM")
    roic = num(metrics, "roicTTM")
    if roic is None:
        roic = num(ratios, "returnOnCapitalEmployedTTM")
    eps = num(metrics, "netIncomePerShareTTM")
    earnings_yield = num(metrics, "earningsYieldTTM")
    pe = num(ratios, "priceEarningsRatioTTM")
    d_to_e = num(metrics, "debtToEquityTTM")
    if d_to_e is None:
        d_to_e = num(ratios, "debtEquityRatioTTM")

    # Conservative-leverage test.
    lev = sig["signals"]["conservative_leverage"]
    if deep:
        inc = first(api.get(f"v3/income-statement/{ticker}", {"period": "annual", "limit": 1}))
        bal = first(api.get(f"v3/balance-sheet-statement/{ticker}", {"period": "annual", "limit": 1}))
        ltd = None
        ni = None
        try:
            ltd = float(bal.get("longTermDebt"))
            ni = float(inc.get("netIncome"))
        except (TypeError, ValueError, AttributeError):
            pass
        if ltd is not None and ni and ni > 0:
            lev_value = ltd / ni
            lev_pass = lev_value < float(lev["exact_value"])
            lev_desc = f"LTD/NI={lev_value:.1f} (<5 exact)"
        else:
            lev_pass = None
            lev_desc = "LTD/NI=n/a"
    else:
        if d_to_e is not None:
            lev_pass = d_to_e < float(lev["approx_value"])
            lev_desc = f"D/E={d_to_e:.2f} (<{lev['approx_value']} proxy)"
        else:
            lev_pass = None
            lev_desc = "D/E=n/a"

    s = sig["signals"]
    checks = {
        "net_margin>15%": (net_margin is not None and net_margin > s["net_margin"]["value"]),
        "roic>=12%": (roic is not None and roic >= s["return_on_capital"]["value"]),
        "leverage_ok": lev_pass,
        "eps>0": (eps is not None and eps > 0),
    }
    roe12 = roe is not None and roe >= 0.12
    roe20 = roe is not None and roe >= 0.20

    core_no_roe = [v for v in checks.values() if v is not None]
    passes_core_no_roe = all(checks[k] for k in checks if checks[k] is not None) and all(
        checks[k] is not None for k in checks
    )

    return {
        "ticker": ticker,
        "net_margin": net_margin,
        "roe": roe,
        "roic": roic,
        "eps": eps,
        "earnings_yield": earnings_yield,
        "pe": pe,
        "leverage_desc": lev_desc,
        "checks": checks,
        "passes_core_ex_roe": passes_core_no_roe,
        "pass_at_roe12": passes_core_no_roe and roe12,
        "pass_at_roe20": passes_core_no_roe and roe20,
    }


def pct(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "n/a"


def build_report(rows: list[dict], y10: float, y10_live: bool, universe_n: int,
                 calls: int, deep: bool, run_date: str) -> str:
    cand12 = [r for r in rows if r["pass_at_roe12"]]
    cand20 = [r for r in rows if r["pass_at_roe20"]]
    cand12.sort(key=lambda r: (r["roic"] or 0), reverse=True)
    cand20.sort(key=lambda r: (r["roic"] or 0), reverse=True)

    L: list[str] = []
    L.append(f"# Buffett quality-compounder scan — {run_date}")
    L.append("")
    L.append("> **These are RESEARCH CANDIDATES, not buys.** Every threshold below is a")
    L.append("> secondary-codifier PROXY (Mary Buffett/Clark, Braverman) for Buffett's own")
    L.append("> C4 moat & intrinsic-value judgment — which is deliberately NOT screened here.")
    L.append("> Confirm each name by hand with `research_checklist.md` before it is investable.")
    L.append("")
    tre = f"{y10*100:.2f}%" + ("" if y10_live else "  *(FALLBACK constant — live 10y unavailable; rebase)*")
    L.append(f"- Universe screened: **{universe_n}**  |  API calls used: **{calls}**  |  "
             f"leverage test: **{'exact LTD/NI' if deep else 'D/E proxy'}**")
    L.append(f"- 10y Treasury (earnings-yield benchmark): **{tre}**")
    L.append(f"- Candidates at ROE ≥ 12% (Mary Buffett): **{len(cand12)}**  |  "
             f"at ROE ≥ 20% (Braverman): **{len(cand20)}**")
    L.append("")

    def table(cands: list[dict], title: str) -> None:
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
                flag = "↑" if ey >= y10 else "↓ (rich)"
                ey_cell = f"{ey*100:.1f}% {flag}"
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
    L.append("_Signals & provenance: see `signals.yaml`. Screen automates the proxy layer")
    L.append("only; moat & intrinsic value remain a human call._")
    return "\n".join(L)


def main() -> int:
    api = Budget()
    sig = load_signals()
    universe = load_universe()
    max_t = int(os.environ.get("MAX_TICKERS", "100"))
    deep = os.environ.get("DEEP", "").strip() in ("1", "true", "yes")
    run_date = os.environ.get("RUN_DATE") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(os.environ.get("OUTPUT_DIR", HERE / "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)

    universe = universe[:max_t]
    print(f"Screening {len(universe)} tickers (deep={deep}) ...")
    y10, y10_live = ten_year_yield(api)

    rows: list[dict] = []
    for i, t in enumerate(universe, 1):
        r = evaluate(t, api, sig, deep)
        if r:
            rows.append(r)
        if i % 10 == 0:
            print(f"  {i}/{len(universe)} done, {api.calls} calls")
        time.sleep(0.2)  # be polite to the API

    report = build_report(rows, y10, y10_live, len(universe), api.calls, deep, run_date)
    out_path = out_dir / f"buffett_scan_{run_date}.md"
    out_path.write_text(report, encoding="utf-8")
    latest = out_dir / "latest.md"
    latest.write_text(report, encoding="utf-8")

    n12 = sum(1 for r in rows if r["pass_at_roe12"])
    n20 = sum(1 for r in rows if r["pass_at_roe20"])
    print(f"\nDone. {n12} candidate(s) at ROE>=12%, {n20} at ROE>=20%. "
          f"{api.calls} API calls.\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
