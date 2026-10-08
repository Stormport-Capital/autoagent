"""Minimal FMP client for this backtest: the /stable API only, a hard call
budget, and a log of every call. The API key comes from FMP_API_KEY and is
never printed or logged (errors go through the dashboard's own `redact`,
tranche-dashboard/data.py:41-48).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone

import requests

import dashpath  # noqa: F401  (tranche-dashboard on sys.path)
from data import redact

BASE = "https://financialmodelingprep.com/stable"
HERE = os.path.dirname(os.path.abspath(__file__))
CALL_LOG = os.path.join(HERE, "cache", "fmp_calls.jsonl")


class BudgetExceeded(RuntimeError):
    pass


class FmpError(RuntimeError):
    pass


class Fmp:
    def __init__(self, max_calls: int, session=None):
        self.key = os.environ.get("FMP_API_KEY")
        if not self.key:
            raise FmpError("FMP_API_KEY is not set")
        self.max_calls = max_calls
        self.calls = 0
        self.bytes = 0
        self.http = session or requests.Session()
        os.makedirs(os.path.dirname(CALL_LOG), exist_ok=True)

    def get(self, path: str, **params):
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"call budget of {self.max_calls} reached")
        self.calls += 1
        t0 = time.time()
        try:
            r = self.http.get(f"{BASE}/{path}", params={**params, "apikey": self.key}, timeout=60)
        except requests.RequestException as e:
            raise FmpError(redact(f"can't reach FMP ({type(e).__name__})")) from None
        self.bytes += len(r.content)
        with open(CALL_LOG, "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "path": path,
                                "params": params, "status": r.status_code, "bytes": len(r.content),
                                "secs": round(time.time() - t0, 2)}) + "\n")
        if r.status_code != 200:
            raise FmpError(redact(f"FMP HTTP {r.status_code} for {path} {params}: {r.text[:200]}"))
        return r.json(), len(r.content)

    def profile(self, symbol: str):
        return self.get("profile", symbol=symbol)

    def chart_5min(self, symbol: str, start: date, end: date):
        """5-minute bars, New York time stamps (FMP), newest first as FMP sends them."""
        return self.get("historical-chart/5min", symbol=symbol, **{"from": start.isoformat(),
                                                                    "to": end.isoformat()})
