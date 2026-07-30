#!/usr/bin/env python3
"""Offline tests for the Buffett screener decision logic and config.

No network, no API key required — exercises the pure `score()` and
`leverage_test()` functions plus validates signals.yaml and universe.txt.
Run: `python test_screener.py` (exit 0 = all passed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import screener

HERE = Path(__file__).resolve().parent
SIG = screener.load_signals()

_failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        _failures.append(name)


def row(net_margin=None, roe=None, roic=None, eps=None, lev_pass=None,
        earnings_yield=None, pe=None):
    values = {
        "net_margin": net_margin, "roe": roe, "roic": roic, "eps": eps,
        "lev_pass": lev_pass, "lev_desc": "test", "earnings_yield": earnings_yield,
        "pe": pe,
    }
    return screener.score("TEST", values, SIG)


def test_scoring():
    print("test_scoring")
    # Clean pass, high ROE -> passes both tiers.
    r = row(net_margin=0.22, roe=0.25, roic=0.18, eps=5.0, lev_pass=True)
    check("strong name passes core", r["passes_core_ex_roe"])
    check("strong name passes ROE>=12", r["pass_at_roe12"])
    check("strong name passes ROE>=20", r["pass_at_roe20"])

    # ROE between the two tiers -> base tier only.
    r = row(net_margin=0.22, roe=0.15, roic=0.18, eps=5.0, lev_pass=True)
    check("mid ROE passes base tier", r["pass_at_roe12"])
    check("mid ROE fails strict tier", not r["pass_at_roe20"])

    # Low ROE -> neither tier.
    r = row(net_margin=0.22, roe=0.10, roic=0.18, eps=5.0, lev_pass=True)
    check("low ROE fails both tiers", not r["pass_at_roe12"] and not r["pass_at_roe20"])

    # Thin margin -> fails core regardless of ROE.
    r = row(net_margin=0.10, roe=0.30, roic=0.18, eps=5.0, lev_pass=True)
    check("thin margin fails core", not r["passes_core_ex_roe"])
    check("thin margin fails both tiers", not r["pass_at_roe12"] and not r["pass_at_roe20"])

    # Boundary: net margin must be strictly > 0.15.
    r = row(net_margin=0.15, roe=0.30, roic=0.18, eps=5.0, lev_pass=True)
    check("net margin exactly 15% fails (strict >)", not r["passes_core_ex_roe"])

    # Boundary: ROIC must be >= 0.12.
    r = row(net_margin=0.22, roe=0.30, roic=0.12, eps=5.0, lev_pass=True)
    check("ROIC exactly 12% passes (>=)", r["passes_core_ex_roe"])
    r = row(net_margin=0.22, roe=0.30, roic=0.119, eps=5.0, lev_pass=True)
    check("ROIC just under 12% fails", not r["passes_core_ex_roe"])

    # Leverage failing kills the name.
    r = row(net_margin=0.22, roe=0.30, roic=0.18, eps=5.0, lev_pass=False)
    check("failing leverage fails core", not r["passes_core_ex_roe"])

    # Missing data is conservative -> fail, never pass.
    r = row(net_margin=0.22, roe=0.30, roic=0.18, eps=5.0, lev_pass=None)
    check("missing leverage (None) fails core", not r["passes_core_ex_roe"])
    r = row(net_margin=None, roe=0.30, roic=0.18, eps=5.0, lev_pass=True)
    check("missing margin fails core", not r["passes_core_ex_roe"])
    r = row(net_margin=0.22, roe=None, roic=0.18, eps=5.0, lev_pass=True)
    check("missing ROE fails both tiers", not r["pass_at_roe12"] and not r["pass_at_roe20"])

    # Negative earnings -> fail.
    r = row(net_margin=0.22, roe=0.30, roic=0.18, eps=-1.0, lev_pass=True)
    check("negative EPS fails core", not r["passes_core_ex_roe"])


def test_leverage_test():
    print("test_leverage_test")
    # Proxy path (deep=False): D/E below threshold passes, above fails.
    p, _ = screener.leverage_test(0.5, None, SIG, deep=False)
    check("D/E 0.5 passes proxy", p is True)
    p, _ = screener.leverage_test(1.5, None, SIG, deep=False)
    check("D/E 1.5 fails proxy", p is False)
    p, d = screener.leverage_test(None, None, SIG, deep=False)
    check("missing D/E -> None (unknown)", p is None and "n/a" in d)
    # Exact path (deep=True): LTD/NI ratio.
    p, _ = screener.leverage_test(None, (100.0, 40.0), SIG, deep=True)  # 2.5x
    check("LTD/NI 2.5x passes exact", p is True)
    p, _ = screener.leverage_test(None, (300.0, 40.0), SIG, deep=True)  # 7.5x
    check("LTD/NI 7.5x fails exact", p is False)
    p, d = screener.leverage_test(None, (100.0, 0.0), SIG, deep=True)   # NI=0
    check("zero net income -> None (unknown)", p is None and "n/a" in d)


def test_pick():
    print("test_pick")
    # pick() tries each candidate field name in order.
    check("picks TTM field", screener.pick({"returnOnEquityTTM": 0.3}, "roe") == 0.3)
    check("falls back to plain field", screener.pick({"returnOnEquity": 0.25}, "roe") == 0.25)
    check("missing -> None", screener.pick({}, "roe") is None)
    check("non-numeric -> None", screener.pick({"netProfitMarginTTM": "n/a"}, "net_margin") is None)


def test_config():
    print("test_config")
    s = SIG["signals"]
    check("net_margin threshold present", isinstance(s["net_margin"]["value"], (int, float)))
    check("roe_gate has two tiers", len(s["roe_gate"]["tiers"]) == 2)
    check("roe tiers ordered ascending", s["roe_gate"]["tiers"][0] < s["roe_gate"]["tiers"][1])
    check("return_on_capital threshold present", "value" in s["return_on_capital"])
    check("conservative_leverage has proxy + exact", "approx_value" in s["conservative_leverage"]
          and "exact_value" in s["conservative_leverage"])
    check("excluded C4 rules listed", len(SIG["excluded_from_screen"]) >= 4)


def test_universe():
    print("test_universe")
    lines = (HERE / "universe.txt").read_text(encoding="utf-8").splitlines()
    tickers = []
    for ln in lines:
        ln = ln.split("#", 1)[0].strip().upper()
        if ln:
            tickers.append(ln)
    check("universe non-empty", len(tickers) > 0)
    check("no duplicate tickers", len(tickers) == len(set(tickers)))
    bad = [t for t in tickers if not all(c.isalnum() or c in ".-" for c in t)]
    check("all tickers well-formed", not bad)


def main() -> int:
    for fn in (test_scoring, test_leverage_test, test_pick, test_config, test_universe):
        fn()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s): {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
