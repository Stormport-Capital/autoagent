"""Trade Review: every closed trade from all three books, with the fields a
trading journal reviews by (strategy, grade, time of day, hold time, exit
type, price / market-cap / float tier, sector) plus your own notes and tags.

Everything here is read-only over the model's trades except the journal
(a note and tags per trade, a note per day), which never touches a price,
a size or a signal.

Sector, industry, market cap and float come from Financial Modeling Prep
(FMP_API_KEY) and are cached in the hourly book's database. They are
TODAY's values, not the values on the trade date: small caps that dilute
can change market cap and float a lot in weeks.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime, timedelta, timezone

import requests

from data import redact
from engine import SLEEVE_LABELS, ValidationError
from indicators import ET

PROFILE_TTL = timedelta(days=7)        # refresh a good profile weekly
PROFILE_RETRY = timedelta(days=1)      # retry a failed lookup daily
MAX_NOTE = 4000
TAG_RE = re.compile(r"^[\w .+&/'-]{1,30}$")


# ----------------------------------------------------------------- buckets
def exit_type(reason: str | None) -> str:
    r = (reason or "").lower()
    if r.startswith("manual flatten") or r.startswith("symbol removed"):
        return "Manual (flatten/remove)"
    if r.startswith("stop"):
        return "Stop"
    if r.startswith("target 2"):
        return "Target 2 (20-MA)"
    if r.startswith("target"):
        return "Target (10-MA)"
    if r.startswith("time stop"):
        return "Time stop"
    if "ema crossed" in r:
        return "Opposite cross"
    return "Other"


def price_tier(p: float) -> str:
    return ("under $1" if p < 1 else "$1-5" if p < 5 else "$5-20" if p < 20 else "$20+")


def cap_tier(v: float | None) -> str:
    if not v:
        return "Unknown"
    return ("Nano (<$50M)" if v < 50e6 else "Micro ($50-300M)" if v < 300e6 else
            "Small ($300M-2B)" if v < 2e9 else "Mid ($2-10B)" if v < 10e9 else "Large (>$10B)")


def float_tier(v: float | None) -> str:
    if not v:
        return "Unknown"
    return "Low (<10M)" if v < 10e6 else "Medium (10-50M)" if v < 50e6 else "High (>50M)"


def time_bucket(t: datetime) -> str:
    """30-minute window of the entry, ET: '09:30-10:00'."""
    m = (t.hour * 60 + t.minute) // 30 * 30
    a = f"{m // 60:02d}:{m % 60:02d}"
    b = f"{(m + 30) // 60:02d}:{(m + 30) % 60:02d}"
    return f"{a}-{b}"


def sessions_held(a: date, b: date) -> int:
    """Weekdays from entry day to exit day, both counted (same day = 1)."""
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return max(n, 1)


def hold_bucket(minutes: float, sessions: int) -> str:
    if sessions <= 1:
        return "Under 1 hour" if minutes < 60 else "Same day"
    return ("2 sessions" if sessions == 2 else "3-5 sessions" if sessions <= 5 else
            "6-10 sessions" if sessions <= 10 else "Over 10 sessions")


def split_tags(raw) -> list[str]:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")
    out = []
    for t in items:
        t = " ".join(str(t).split()).lower()
        if t and t not in out:
            if not TAG_RE.match(t):
                raise ValidationError(f"tag {t!r}: letters, numbers, spaces and . + & / ' - "
                                      f"only, up to 30 characters")
            out.append(t)
    return out[:20]


# ------------------------------------------------------------------ trades
def closed_trades(books: list, profiles: dict) -> list[dict]:
    """books: [(key, label, store)]. One row per closed tranche (a VWAP
    scale-out is two rows: the half covered at the 10-MA and the rest)."""
    out = []
    for key, label, store in books:
        grades = {r["id"]: r["grade"] for r in store.q("SELECT id, grade FROM symbols")}
        for t in store.q("SELECT * FROM tranches WHERE status='closed'"):
            opened = datetime.fromisoformat(t["entry_time"]).astimezone(ET)
            closed = datetime.fromisoformat(t["exit_time"]).astimezone(ET)
            net = t["gross_pnl"] - t["borrow_fees"]
            minutes = (closed - opened).total_seconds() / 60
            sess = sessions_held(opened.date(), closed.date())
            prof = profiles.get(t["symbol"]) or {}
            strategy = SLEEVE_LABELS.get(t["sleeve"], t["sleeve"])
            if t["sleeve"] == "VWAP":
                strategy += " - opening fade" if t.get("trigger") == "open_fade" else " - Trigger B"
            grade = t.get("grade")
            out.append({
                "book": key, "book_label": label, "id": t["id"], "symbol": t["symbol"],
                "strategy": strategy, "side": t["side"], "qty": t["qty"],
                "entry_time": opened.isoformat(), "exit_time": closed.isoformat(),
                "entry_day": opened.date().isoformat(), "exit_day": closed.date().isoformat(),
                "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                "notional": t["entry_price"] * t["qty"], "risk": t["risk_dollars"],
                "gross": t["gross_pnl"], "fees": t["borrow_fees"], "net": net,
                "r": net / t["risk_dollars"] if t["risk_dollars"] else None,
                "hold_min": round(minutes), "sessions": sess,
                "hold": hold_bucket(minutes, sess),
                "time_of_day": time_bucket(opened), "weekday": opened.strftime("%a"),
                "exit_type": exit_type(t["exit_reason"]), "exit_reason": t["exit_reason"],
                # grade at entry; trades opened before it was recorded use the symbol's current grade
                "grade": grade or grades.get(t["symbol_id"]) or "Custom risk",
                "grade_at_entry": grade is not None,
                "price_tier": price_tier(t["entry_price"]),
                "sector": prof.get("sector") or "Unknown",
                "industry": prof.get("industry") or "Unknown",
                "market_cap": prof.get("market_cap"), "cap_tier": cap_tier(prof.get("market_cap")),
                "float_shares": prof.get("float_shares"),
                "float_tier": float_tier(prof.get("float_shares")),
                "note": t.get("note") or "", "tags": split_tags(t.get("tags") or ""),
            })
    out.sort(key=lambda r: (r["exit_time"], r["book"], r["id"]))
    return out


def save_journal(store, trade_id: int, note, tags) -> dict:
    note = str(note or "").strip()
    if len(note) > MAX_NOTE:
        raise ValidationError(f"note is limited to {MAX_NOTE} characters")
    tags = split_tags(tags)
    if not store.q("SELECT 1 FROM tranches WHERE id=?", (trade_id,)):
        raise ValidationError("unknown trade")
    store.x("UPDATE tranches SET note=?, tags=? WHERE id=?",
            (note or None, ",".join(tags) or None, trade_id))
    return {"note": note, "tags": tags}


def day_notes(store) -> dict[str, str]:
    return {r["day"]: r["note"] for r in store.q("SELECT day, note FROM day_notes")}


def save_day_note(store, day: str, note) -> None:
    try:
        date.fromisoformat(day or "")
    except ValueError:
        raise ValidationError("day must be YYYY-MM-DD")
    note = str(note or "").strip()
    if len(note) > MAX_NOTE:
        raise ValidationError(f"note is limited to {MAX_NOTE} characters")
    if note:
        store.x("INSERT OR REPLACE INTO day_notes VALUES (?, ?)", (day, note))
    else:
        store.x("DELETE FROM day_notes WHERE day=?", (day,))


# ---------------------------------------------------------------- profiles
class FmpProfiles:
    """Sector / industry / market cap from FMP's profile, float from its
    shares-float endpoint. Stable API first, legacy v3 as the fallback."""

    PROFILE = [("https://financialmodelingprep.com/stable/profile", True),
               ("https://financialmodelingprep.com/api/v3/profile/{sym}", False)]
    FLOAT = [("https://financialmodelingprep.com/stable/shares-float", True),
             ("https://financialmodelingprep.com/api/v4/shares_float", True)]

    def __init__(self, key: str, session=None):
        self.key, self.http = key, session or requests

    def _first(self, attempts, symbol) -> dict | None:
        last = "no response"
        for url, by_param in attempts:
            params = {"apikey": self.key, **({"symbol": symbol} if by_param else {})}
            try:
                r = self.http.get(url.format(sym=symbol), params=params, timeout=15)
            except requests.RequestException as e:
                last = f"can't reach FMP ({type(e).__name__})"
                continue
            if r.status_code != 200:
                last = f"FMP HTTP {r.status_code}"
                continue
            body = r.json()
            if isinstance(body, list):
                return body[0] if body else None
            last = f"FMP returned {str(body)[:120]}"
        raise RuntimeError(last)

    def fetch(self, symbol: str) -> dict:
        p = self._first(self.PROFILE, symbol) or {}
        try:
            f = self._first(self.FLOAT, symbol) or {}
        except RuntimeError:
            f = {}  # float is optional; the profile alone is still useful
        num = lambda *vals: next((float(v) for v in vals if v not in (None, "", 0)), None)
        if not p and not f:
            raise RuntimeError("FMP has no profile for this ticker (renamed or delisted?)")
        return {"sector": p.get("sector") or None, "industry": p.get("industry") or None,
                "market_cap": num(p.get("marketCap"), p.get("mktCap")),
                "float_shares": num(f.get("floatShares"), f.get("float"))}


class ProfileCache:
    def __init__(self, store, fetcher=None):
        self.store, self.lock, self.busy = store, threading.Lock(), False
        key = os.environ.get("FMP_API_KEY")
        self.fetcher = fetcher if fetcher is not None else (FmpProfiles(key) if key else None)

    def all(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in self.store.q("SELECT * FROM profiles")}

    def stale(self, symbols, now: datetime) -> list[str]:
        have = self.all()
        out = []
        for s in sorted(set(symbols)):
            r = have.get(s)
            if r is None:
                out.append(s)
                continue
            age = now - datetime.fromisoformat(r["fetched_at"])
            if age > (PROFILE_RETRY if r["error"] else PROFILE_TTL):
                out.append(s)
        return out

    def refresh(self, symbols, now: datetime | None = None) -> int:
        """Fetch missing/stale profiles. Returns how many were looked up."""
        if self.fetcher is None:
            return 0
        now = now or datetime.now(timezone.utc)
        with self.lock:
            todo = self.stale(symbols, now)
            for s in todo:
                try:
                    p, err = self.fetcher.fetch(s), None
                except Exception as e:  # one bad ticker must not stop the rest
                    p, err = {}, redact(e)[:200]
                self.store.x("""INSERT OR REPLACE INTO profiles (symbol, sector, industry,
                                market_cap, float_shares, fetched_at, error)
                                VALUES (?,?,?,?,?,?,?)""",
                             (s, p.get("sector"), p.get("industry"), p.get("market_cap"),
                              p.get("float_shares"), now.isoformat(), err))
            return len(todo)

    def refresh_async(self, symbols) -> bool:
        """Start a background refresh unless one is already running."""
        if self.fetcher is None or self.busy or not self.stale(symbols, datetime.now(timezone.utc)):
            return False
        self.busy = True

        def run():
            try:
                self.refresh(symbols)
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()
        return True

    def status(self) -> str:
        if self.fetcher is None:
            return "Sector, market cap and float need FMP_API_KEY in .env"
        if self.busy:
            return "Looking up sector / market cap / float - refresh in a minute"
        errs = self.store.q("SELECT COUNT(*) n FROM profiles WHERE error IS NOT NULL")[0]["n"]
        return f"{errs} ticker(s) had no FMP profile" if errs else ""


def review_payload(books: list, cache: ProfileCache) -> dict:
    """books: [(key, label, store)]; the first book's store holds day notes
    and the profile cache."""
    symbols = {r["symbol"] for _k, _l, st in books
               for r in st.q("SELECT DISTINCT symbol FROM tranches WHERE status='closed'")}
    cache.refresh_async(symbols)
    open_n = sum(st.q("SELECT COUNT(*) n FROM tranches WHERE status='open'")[0]["n"]
                 for _k, _l, st in books)
    return {
        "books": [{"key": k, "label": l} for k, l, _s in books],
        "trades": closed_trades(books, cache.all()),
        "open_trades": open_n,
        "day_notes": day_notes(books[0][2]),
        "profiles_note": cache.status(),
    }
