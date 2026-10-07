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
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

import requests

from indicators import ET, RTH_OPEN, Bar

FIVE_MIN = timedelta(minutes=5)


class DataError(RuntimeError):
    pass


SECRET_ENV = ("POLYGON_API_KEY", "FMP_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
              "APCA_15M_API_KEY_ID", "APCA_15M_API_SECRET_KEY",
              "APCA_5M_API_KEY_ID", "APCA_5M_API_SECRET_KEY")


def redact(msg: str) -> str:
    """Strip API keys from any text that may be shown, logged or pasted."""
    msg = re.sub(r"(?i)(apikey|api_key|token)=[^&\s'\"]+", r"\1=***", str(msg))
    for k in SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 4:
            msg = msg.replace(v, "***")
    return msg


def _first_rth_open(rows, day: date) -> float | None:
    """rows: (start datetime, open) in time order -> open of the 9:30 minute."""
    for start, o in rows:
        local = start.astimezone(ET)
        if local.date() == day and local.time() >= RTH_OPEN and o is not None:
            return float(o)
    return None


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


    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        r = requests.get(self.URL.format(sym=symbol),
                         params={"interval": "1m", "range": "1d", "includePrePost": "false"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code != 200:
            return None
        try:
            res = r.json()["chart"]["result"][0]
            opens = res["indicators"]["quote"][0]["open"]
            rows = [(datetime.fromtimestamp(t, tz=timezone.utc), opens[i])
                    for i, t in enumerate(res["timestamp"])]
        except (KeyError, IndexError, TypeError):
            return None
        return _first_rth_open(rows, day)


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


    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        url = f"{self.base}/v2/aggs/ticker/{symbol}/range/1/minute/{day.isoformat()}/{day.isoformat()}"
        try:
            r = requests.get(url, params={"adjusted": "false", "sort": "asc", "limit": 50000},
                             headers={"Authorization": f"Bearer {self.key}"}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        rows = [(datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc), b["o"])
                for b in r.json().get("results") or []]
        return _first_rth_open(rows, day)


class FmpProvider:
    """Financial Modeling Prep intraday bars (FMP_API_KEY).

    Uses the "stable" API only. (The legacy v3 path answers every account
    opened after Aug 31, 2025 with a 403 that hid the real error.)
    FMP stamps intraday bars in exchange (New York) time; by default the stamp
    is taken as the bar START. If the Bars table shows every bar shifted by
    5 minutes against your charts, set FMP_BAR_TIME=end in .env.
    Keeps a per-symbol cache and only re-fetches from the last day it holds.
    Counts requests and bytes per ET day and prints them to the log once an
    hour and at the end of each day, so FMP's data allowance can be checked.
    """

    name = "fmp"
    STABLE = "https://financialmodelingprep.com/stable/historical-chart/{tf}"

    def __init__(self, session=None):
        self.key = os.environ.get("FMP_API_KEY")
        if not self.key:
            raise DataError("Set FMP_API_KEY for the fmp provider")
        self.shift = {"start": 0, "end": -1}.get(os.environ.get("FMP_BAR_TIME", "start").lower(), 0)
        self.http = session or requests
        self._bars: dict[str, dict[datetime, Bar]] = {}
        self._lock = threading.Lock()
        self.usage = {"day": None, "hour": None, "calls": 0, "bytes": 0, "errors": 0}

    def _count(self, nbytes: int, error: bool) -> None:
        """Per-ET-day request and byte totals, printed hourly and at day end."""
        now = datetime.now(ET)
        with self._lock:
            u = self.usage
            if u["day"] is not None and u["day"] != now.date():
                print(self.usage_line(final=True), flush=True)
                u.update(calls=0, bytes=0, errors=0, hour=None)
            u["day"] = now.date()
            u["calls"] += 1
            u["bytes"] += nbytes
            u["errors"] += int(error)
            if u["hour"] != now.hour:
                if u["hour"] is not None:
                    print(self.usage_line(), flush=True)
                u["hour"] = now.hour

    def usage_line(self, final: bool = False) -> str:
        u = self.usage
        return (f"FMP usage {u['day']}{'' if final else ' so far'}: {u['calls']} requests, "
                f"{u['bytes'] / 1e6:.1f} MB, {u['errors']} errors")

    @staticmethod
    def _reason(status: int, body) -> str:
        msg = body.get("Error Message") or body.get("message") if isinstance(body, dict) else None
        msg = str(msg or body)[:200]
        if status == 429:
            return f"FMP usage or rate limit reached (HTTP 429): {msg}"
        if status == 401:
            return f"FMP rejected the API key (HTTP 401): {msg}"
        if status in (402, 403):
            return f"FMP refused: plan, usage or permission (HTTP {status}): {msg}"
        return f"FMP HTTP {status}: {msg}"

    def _rows(self, tf: str, symbol: str, start: date, end: date) -> list[dict]:
        params = {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(),
                  "apikey": self.key}
        try:
            r = self.http.get(self.STABLE.format(tf=tf), params=params, timeout=20)
        except requests.RequestException as e:
            self._count(0, True)
            raise DataError(f"can't reach FMP ({type(e).__name__}) for {symbol}")
        size = len(getattr(r, "content", b"") or b"") or len(getattr(r, "text", "") or "")
        try:
            body = r.json()
        except ValueError:
            body = (getattr(r, "text", "") or "")[:200]
        if r.status_code == 200 and isinstance(body, list):
            self._count(size, False)
            return body
        self._count(size, True)
        if r.status_code == 200:
            raise DataError(redact(f"FMP returned {str(body)[:200]} for {symbol}"))
        raise DataError(redact(f"{self._reason(r.status_code, body)} for {symbol}"))

    def _to_bar(self, r: dict, step: timedelta) -> Bar | None:
        try:
            t = datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            start = t + self.shift * step
            return Bar(start, start + step, float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            return None

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        today = now.astimezone(ET).date()
        with self._lock:
            have = self._bars.setdefault(symbol, {})
            # first fetch: ~35 days; after that, only from the last day already held
            first = today - timedelta(days=35) if not have else \
                min(today, max(b.start for b in have.values()).astimezone(ET).date())
        chunks, d = [], first
        while d <= today:
            e = min(d + timedelta(days=6), today)
            chunks.append((d, e))
            d = e + timedelta(days=1)
        fetched = []
        for a, b in chunks:
            for r in self._rows("5min", symbol, a, b):
                bar = self._to_bar(r, FIVE_MIN)
                if bar:
                    fetched.append(bar)
        with self._lock:
            for bar in fetched:
                have[bar.start] = bar
            lo = now - timedelta(days=35)
            return sorted((x for x in have.values() if lo <= x.start and x.end <= now),
                          key=lambda x: x.start)

    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        try:
            rows = self._rows("1min", symbol, day, day)
        except DataError:
            return None
        bars = [b for b in (self._to_bar(r, timedelta(minutes=1)) for r in rows) if b]
        return _first_rth_open(sorted(((b.start, b.open) for b in bars)), day)


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


    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        start = datetime.combine(day, RTH_OPEN, tzinfo=ET)
        try:
            r = requests.get(self.URL.format(sym=symbol), timeout=20, params={
                "timeframe": "1Min", "start": start.astimezone(timezone.utc).isoformat(),
                "limit": 5, "adjustment": "raw", "feed": self.feed},
                headers={"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
        except requests.RequestException:
            return None
        if r.status_code != 200:
            return None
        rows = [(datetime.fromisoformat(b["t"].replace("Z", "+00:00")), b["o"])
                for b in r.json().get("bars") or []]
        return _first_rth_open(rows, day)


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


    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        self.five_min_bars(symbol, now)  # make sure the day is generated
        for b in self._cache.get(symbol, []):
            if b.session == day:
                return b.open  # the opening print exists from 9:30
        return None


class CachedProvider:
    """Shares one fetch per symbol between the books: a fetch is reused for
    up to ttl_s seconds while the clock is still in the same 5-minute window
    (no new 5-minute bar can have completed in between)."""

    def __init__(self, inner, ttl_s: float = 60.0):
        self.inner, self.ttl_s = inner, ttl_s
        self.name = inner.name
        self._cache: dict = {}
        self._lock = threading.Lock()

    def five_min_bars(self, symbol: str, now: datetime) -> list[Bar]:
        window = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
        key = (symbol, window)
        with self._lock:
            hit = self._cache.get(key)
            if hit and _time.monotonic() - hit[0] < self.ttl_s:
                return [b for b in hit[1] if b.end <= now]
        bars = self.inner.five_min_bars(symbol, now)
        with self._lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = (_time.monotonic(), bars)
        return bars

    def session_open(self, symbol: str, day: date, now: datetime) -> float | None:
        getter = getattr(self.inner, "session_open", None)
        return getter(symbol, day, now) if getter else None


def make_provider(name: str, demo_origin: date | None = None):
    if name == "yahoo":
        return YahooProvider()
    if name == "polygon":
        return PolygonProvider()
    if name == "fmp":
        return FmpProvider()
    if name == "alpaca":
        return AlpacaProvider()
    if name == "demo":
        return DemoProvider(demo_origin or date.today() - timedelta(days=90))
    raise ValueError(f"unknown provider {name!r}")
