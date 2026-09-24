"""Market-data providers. Each returns 5-minute bars; hourly bars and VWAP are
derived from them in indicators.py so every provider behaves identically.

  polygon - needs POLYGON_API_KEY. Consolidated (all-exchange) volume, so VWAP
            is accurate. Real-time on paid real-time plans; Starter-tier data
            is 15-minute delayed (the engine waits for each hour's data).
  yahoo   - no key; unofficial chart endpoint (can rate-limit or change).
  alpaca  - needs APCA_API_KEY_ID / APCA_API_SECRET_KEY. Free plan = IEX feed,
            whose volume is a small slice of the tape, so VWAP is approximate.
            Set ALPACA_DATA_FEED=sip if your plan includes consolidated data.
  demo    - deterministic synthetic prices; no network. For trying the app.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import re
from datetime import date, datetime, timedelta, timezone

import requests

from indicators import ET, RTH_OPEN, Bar

FIVE_MIN = timedelta(minutes=5)


class DataError(RuntimeError):
    pass


SECRET_ENV = ("POLYGON_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def redact(msg: str) -> str:
    """Strip API keys from any text that may be shown, logged or pasted."""
    msg = re.sub(r"(?i)(apikey|api_key|token)=[^&\s'\"]+", r"\1=***", str(msg))
    for k in SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 4:
            msg = msg.replace(v, "***")
    return msg


class YahooProvider:
    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        r = requests.get(
            self.URL.format(sym=symbol),
            params={"interval": "5m", "range": "1mo", "includePrePost": "false"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        if r.status_code != 200:
            raise DataError(f"Yahoo HTTP {r.status_code} for {symbol}")
        try:
            res = r.json()["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise DataError(f"Yahoo returned no data for {symbol}") from e
        bars = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            start = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(ET)
            if start + FIVE_MIN <= now:
                bars.append(Bar(start, start + FIVE_MIN, o, h, l, c, float(v or 0)))
        return bars


class PolygonProvider:
    name = "polygon"

    def __init__(self):
        self.key = os.environ.get("POLYGON_API_KEY")
        self.base = os.environ.get("POLYGON_BASE_URL", "https://api.polygon.io").rstrip("/")
        if not self.key:
            raise DataError("Set POLYGON_API_KEY for the polygon provider")

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        start = (now - timedelta(days=35)).date().isoformat()
        url = (f"{self.base}/v2/aggs/ticker/{symbol}/range/5/minute/"
               f"{start}/{now.date().isoformat()}")
        params = {"adjusted": "false", "sort": "asc", "limit": 50000}
        headers = {"Authorization": f"Bearer {self.key}"}  # never in the URL
        bars: list[Bar] = []
        while url:
            try:
                r = requests.get(url, params=params, headers=headers, timeout=20)
            except requests.RequestException as e:
                raise DataError(f"can't reach Polygon ({type(e).__name__})") from None
            if r.status_code != 200:
                raise DataError(redact(f"Polygon HTTP {r.status_code} for {symbol}: {r.text[:200]}"))
            body = r.json()
            for b in body.get("results") or []:
                t0 = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).astimezone(ET)
                if t0 + FIVE_MIN <= now:
                    bars.append(Bar(t0, t0 + FIVE_MIN, b["o"], b["h"], b["l"], b["c"],
                                    float(b.get("v") or 0)))
            url = body.get("next_url")
            params = None  # next_url already carries the query and cursor
        return bars


class AlpacaProvider:
    name = "alpaca"
    URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"

    def __init__(self):
        self.key = os.environ.get("APCA_API_KEY_ID")
        self.secret = os.environ.get("APCA_API_SECRET_KEY")
        self.feed = os.environ.get("ALPACA_DATA_FEED", "iex")
        if not (self.key and self.secret):
            raise DataError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for the alpaca provider")

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        params = {
            "timeframe": "5Min",
            "start": (now - timedelta(days=35)).astimezone(timezone.utc).isoformat(),
            "end": now.astimezone(timezone.utc).isoformat(),
            "limit": 10000,
            "adjustment": "raw",
            "feed": self.feed,
        }
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        bars: list[Bar] = []
        while True:
            r = requests.get(self.URL.format(sym=symbol), params=params,
                             headers=headers, timeout=20)
            if r.status_code != 200:
                raise DataError(f"Alpaca HTTP {r.status_code} for {symbol}: {r.text[:200]}")
            body = r.json()
            for b in body.get("bars") or []:
                start = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
                if start + FIVE_MIN <= now:
                    bars.append(Bar(start, start + FIVE_MIN, b["o"], b["h"],
                                    b["l"], b["c"], float(b["v"])))
            token = body.get("next_page_token")
            if not token:
                return bars
            params["page_token"] = token


class DemoProvider:
    """Synthetic regime-switching random walk, deterministic per symbol.

    Prices trend up and down in multi-hour regimes so EMA crosses and VWAP
    losses actually occur. Weekends are skipped; holidays are not modelled.
    """

    name = "demo"

    def __init__(self, origin: date):
        self.origin = origin
        self._cache: dict[str, list[Bar]] = {}

    def _generate(self, symbol: str, through: date) -> list[Bar]:
        seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        price = 20 + (seed % 180)
        drift = 0.0
        bars: list[Bar] = []
        day = self.origin
        while day <= through:
            if day.weekday() < 5:
                t = datetime.combine(day, RTH_OPEN, tzinfo=ET)
                price *= math.exp(rng.gauss(0, 0.01))  # overnight gap
                for _ in range(78):
                    if rng.random() < 0.03:
                        drift = rng.choice([-1, 1]) * rng.uniform(0.0004, 0.0015)
                    o = price
                    c = o * math.exp(drift + rng.gauss(0, 0.003))
                    h = max(o, c) * (1 + abs(rng.gauss(0, 0.0015)))
                    lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0015)))
                    v = rng.randint(20_000, 200_000)
                    bars.append(Bar(t, t + FIVE_MIN, round(o, 4), round(h, 4),
                                    round(lo, 4), round(c, 4), float(v)))
                    price = c
                    t += FIVE_MIN
            day += timedelta(days=1)
        return bars

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        through = now.astimezone(ET).date()
        cached = self._cache.get(symbol)
        if not cached or cached[-1].session < through:
            self._cache[symbol] = cached = self._generate(symbol, through + timedelta(days=7))
        lo = now - timedelta(days=35)
        return [b for b in cached if lo <= b.start and b.end <= now]


def make_provider(name: str, demo_origin: date | None = None):
    if name == "yahoo":
        return YahooProvider()
    if name == "polygon":
        return PolygonProvider()
    if name == "alpaca":
        return AlpacaProvider()
    if name == "demo":
        return DemoProvider(demo_origin or date.today() - timedelta(days=90))
    raise ValueError(f"unknown provider {name!r}")
