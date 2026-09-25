"""Tranche dashboard: local web server + schedulers for two books.

Two independent books run side by side with identical rules: "1h" on hourly
bars and "15m" on 15-minute bars. Each has its own database, settings, capital,
performance and (optionally) its own Alpaca paper account.

    python app.py                      # live prices from Yahoo, http://127.0.0.1:8050
    python app.py --provider polygon   # Polygon data (POLYGON_API_KEY), recommended
    python app.py --provider alpaca    # Alpaca market data (keys via env)
    python app.py --demo               # synthetic prices on a fast simulated clock

The model is simulated. Orders go only to an Alpaca PAPER account, only when
linked (keys in .env) and switched on in the dashboard; see broker.py / SETUP.md.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hmac
import tempfile
import html
import io
import json
import os
import re
import threading
import time as _time
import webbrowser

import requests
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backup import Backups, backup_folder
from broker import AlpacaPaper, BrokerError, BrokerSync
from data import make_provider, redact
from engine import SLEEVE_LABELS, Engine, ValidationError
from data import CachedProvider
from indicators import ET, RTH_OPEN, session_bar_ends
from store import Store

HERE = Path(__file__).parent
RETRY_EVERY = timedelta(minutes=3)
RETRY_WINDOW = timedelta(minutes=40)

# (key, bar minutes, label, env prefix for its Alpaca paper keys, db file)
BOOKS = (
    ("1h", 60, "Hourly", "APCA", "tranche.db"),
    ("15m", 15, "15-min", "APCA_15M", "tranche_15m.db"),
)


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use,
    # which would let two copies trade at once. Only allow reuse elsewhere.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


KEYS = ("POLYGON_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
OPTIONAL_KEYS = ("FMP_API_KEY",  # second data source, compared at startup
                 "APCA_15M_API_KEY_ID", "APCA_15M_API_SECRET_KEY")  # 15-min book's paper account


def load_dotenv(path: Path) -> list[str]:
    """Load KEY=VALUE lines from .env (a non-empty real environment variable
    wins). Returns human-readable notes on what was found, never the values."""
    notes = []
    if not path.exists():
        alt = path.with_name(path.name + ".txt")
        notes.append(f"{path} not found" + (f" - but {alt.name} exists: rename it to .env"
                                            if alt.exists() else ""))
        return notes
    empty, values, seen = [], {}, {}
    for n, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        seen.setdefault(k, []).append(n)
        if not v:
            empty.append(k)
        else:
            values[k] = v  # the last line for a key wins, as in other .env loaders
    for k, v in values.items():
        if not os.environ.get(k):
            os.environ[k] = v
    for k, lines in seen.items():
        if len(lines) > 1:
            notes.append(f"{k}: set on {len(lines)} lines ({', '.join(map(str, lines))}) - "
                         f"the last one is used; delete the others")
    for k in KEYS:
        if os.environ.get(k):
            notes.append(f"{k}: found ({len(os.environ[k])} chars)")
        elif k in empty:
            notes.append(f"{k}: EMPTY in .env - paste the key after the = and save")
        else:
            notes.append(f"{k}: missing from .env")
    for k in OPTIONAL_KEYS:
        if os.environ.get(k):
            notes.append(f"{k}: found ({len(os.environ[k])} chars)")
        else:
            notes.append(f"{k}: not set (optional)")
    return notes


class RealClock:
    demo = False

    def now(self) -> datetime:
        return datetime.now(ET)


class SimClock:
    """Starts `days` weekdays ago and jumps one hourly bar per step."""

    demo = True

    def __init__(self, days: int, delay_min: int, minutes: int = 15):
        d = datetime.now(ET).date()
        while days:
            d -= timedelta(days=1)
            days -= d.weekday() < 5
        self._now = datetime.combine(d, time(9, 0), tzinfo=ET)
        self.delay = timedelta(minutes=delay_min)
        self.minutes = minutes
        self.lock = threading.Lock()

    def now(self) -> datetime:
        with self.lock:
            return self._now

    def step(self) -> datetime:
        with self.lock:
            nxt = next_boundary(self._now, self.delay, self.minutes)
            self._now = nxt
            return nxt


def boundaries(day: date, delay: timedelta, minutes: int = 60) -> list[datetime]:
    """Check times: the 9:30 open (acts on yesterday's last bar), then each
    intraday bar close - hourly 10:00 ... 15:00, 15-min 9:45 ... 15:45. The
    last bar of the day is not checked at 16:00; it executes at the next open."""
    if day.weekday() >= 5:
        return []
    ends = session_bar_ends(day, minutes)[:-1]
    return [datetime.combine(day, RTH_OPEN, tzinfo=ET) + delay] + [e + delay for e in ends]


def prev_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def bar_for_boundary(boundary: datetime, delay: timedelta) -> tuple[datetime, datetime]:
    """(end of the bar a check handles, when that bar's signals execute)."""
    t = boundary - delay
    if t.time() == RTH_OPEN:
        prev_close = datetime.combine(prev_weekday(t.date()), time(16, 0), tzinfo=ET)
        return prev_close, t
    return t, t


def next_boundary(now: datetime, delay: timedelta, minutes: int = 60) -> datetime:
    d = now.date()
    while True:
        for b in boundaries(d, delay, minutes):
            if b > now:
                return b
        d += timedelta(days=1)


class Scheduler(threading.Thread):
    """One book's clock: a tick at the 9:30 open (for yesterday's last bar)
    and as each intraday bar closes."""

    def __init__(self, engine: Engine, clock, sync: BrokerSync | None = None):
        super().__init__(daemon=True)
        self.engine, self.clock = engine, clock
        self.minutes = engine.minutes
        self.sync = sync  # never set in demo mode
        self.last_tick: datetime | None = None
        self.last_error: str | None = None

    def delay(self) -> timedelta:
        return timedelta(minutes=self.engine.store.settings()["bar_close_delay_min"])

    def run_tick(self) -> None:
        now = self.clock.now()
        try:
            self.engine.tick(now)
            self.last_error = None
        except Exception as e:  # keep the loop alive; surface on the dashboard
            self.last_error = redact(e)
        self.last_tick = now
        self.run_sync()

    def run_sync(self) -> None:
        if self.sync and self.engine.store.settings()["broker_sync_enabled"]:
            self.sync.sync(self.clock.now())

    def retry_due(self, now: datetime, boundary: datetime) -> bool:
        """Re-check every 3 min for up to 40 min after a boundary while some
        symbol is still missing that hour (15-minute-delayed data plans)."""
        window = min(RETRY_WINDOW, timedelta(minutes=self.minutes))
        return (now - boundary <= window
                and self.last_tick is not None and now - self.last_tick >= RETRY_EVERY
                and self.engine.behind(*bar_for_boundary(boundary, self.delay())))

    def next_tick(self) -> datetime:
        return next_boundary(self.clock.now(), self.delay(), self.minutes)

    def run(self) -> None:
        self.run_tick()  # catch up on bars completed while the app was down
        while True:
            _time.sleep(20)
            now = self.clock.now()
            due = [b for b in boundaries(now.date(), self.delay(), self.minutes) if b <= now]
            if due and (self.last_tick is None or self.last_tick < due[-1]):
                self.run_tick()
            elif due and self.retry_due(now, due[-1]):
                self.run_tick()  # the feed hadn't finished that hour yet: look again
            elif self.sync and self.sync.needs_followup and self.sync.last_sync \
                    and (now - self.sync.last_sync).total_seconds() >= 60:
                self.run_sync()  # an order was still working: finish the job


class DemoDriver(threading.Thread):
    """Demo only: steps the simulated clock one 15-minute boundary at a time
    and runs every book (a book with no new bar simply finds nothing to do)."""

    def __init__(self, clock, scheds: list, speed: float):
        super().__init__(daemon=True)
        self.clock, self.scheds, self.speed = clock, scheds, speed
        self.paused = False

    def run(self) -> None:
        while True:
            _time.sleep(self.speed)
            if self.clock.now() >= datetime.now(ET):
                self.paused = True  # replay has caught up with the present
            if not self.paused:
                self.clock.step()
                for s in self.scheds:
                    s.run_tick()


class Book:
    def __init__(self, key: str, minutes: int, label: str, engine: Engine, sched: Scheduler,
                 link_note: str | None = None):
        self.key, self.minutes, self.label = key, minutes, label
        self.engine, self.sched = engine, sched
        self.link_note = link_note  # why the paper link is off, if it is


def _short(e: Exception) -> str:
    if isinstance(e, requests.RequestException):
        return f"can't connect ({type(e).__name__}) - check your internet connection"
    return redact(e)[:200]


def extra_sources(active: str) -> list:
    """Other data sources with keys configured, so startup can compare them."""
    out = []
    for name, env in (("polygon", "POLYGON_API_KEY"), ("fmp", "FMP_API_KEY")):
        if name != active and os.environ.get(env):
            try:
                out.append(make_provider(name))
            except Exception:
                pass
    return out


def connection_check(provider, papers: list, now: datetime) -> list[str]:
    """One live call to each service, so the launcher window shows whether the
    keys actually work (not just whether they are present)."""
    lines = []
    sources = [provider] + [p for p in extra_sources(provider.name)]
    for src in sources:
        tag = "in use" if src is provider else "not in use"
        try:
            bars = src.five_min_bars("SPY", now)
            if bars:
                lag = (now - bars[-1].end).total_seconds() / 60
                speed = ""
                if market_open(now):
                    speed = f" ({lag:.0f} min ago - " + ("real-time" if lag < 8 else "DELAYED") + ")"
                lines.append(f"{src.name} [{tag}]: OK - SPY data through "
                             f"{bars[-1].end.astimezone(ET):%a %H:%M} ET{speed}")
            else:
                lines.append(f"{src.name} [{tag}]: connected, but no SPY bars came back")
        except Exception as e:
            lines.append(f"{src.name} [{tag}]: FAILED - {redact(e)[:200]}")
    if len(sources) > 1:
        lines.append("to switch data source, add TRANCHE_PROVIDER=polygon or "
                     "TRANCHE_PROVIDER=fmp to .env and restart")
    for label, paper, note in papers:
        if paper is None:
            lines.append(f"Alpaca paper ({label}): not linked - {note}")
            continue
        try:
            a = paper.account()
            lines.append(f"Alpaca paper ({label}): OK - equity ${float(a['equity']):,.2f}, "
                         f"shorting {'enabled' if a.get('shorting_enabled') else 'DISABLED'}"
                         + (", TRADING BLOCKED" if a.get("trading_blocked") else ""))
        except Exception as e:
            lines.append(f"Alpaca paper ({label}): FAILED - {_short(e)}")
    return lines


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and RTH_OPEN <= now.time() < time(16, 0)


def build_state(book: Book, books: list, provider_name: str, demo) -> dict:
    engine, sched = book.engine, book.sched
    store = engine.store
    now = sched.clock.now()
    summ = engine.summary()
    symbols = store.q("SELECT * FROM symbols WHERE status!='removed' ORDER BY symbol")
    by_sym: dict[int, dict] = {}
    for p in summ["open"]:
        by_sym.setdefault(p["symbol_id"], {})[p["sleeve"]] = p
    for s in symbols:
        s["snapshot"] = json.loads(s["snapshot"]) if s["snapshot"] else None
        s["tranches"] = by_sym.get(s["id"], {})
        s["unrealized"] = sum(t["unrealized"] - t["borrow_fees"] for t in s["tranches"].values())
    return {
        "book": {"key": book.key, "label": book.label, "minutes": book.minutes},
        "books": [{"key": b.key, "label": b.label, "equity": b.engine.equity(),
                   "start": b.engine.store.settings()["starting_capital"],
                   "linked": b.sched.sync is not None,
                   "sync_on": b.engine.store.settings()["broker_sync_enabled"]} for b in books],
        "now": now.isoformat(),
        "demo": sched.clock.demo,
        "paused": bool(demo and demo.paused),
        "provider": provider_name,
        "market_open": market_open(now),
        "last_tick": sched.last_tick.isoformat() if sched.last_tick else None,
        "next_tick": sched.next_tick().isoformat(),
        "scheduler_error": sched.last_error,
        "settings": store.settings(),
        "sleeves": SLEEVE_LABELS,
        "symbols": symbols,
        "summary": summ,
        "events": store.q("SELECT * FROM events ORDER BY id DESC LIMIT 300"),
        "broker": broker_state(book),
    }


def broker_state(book: Book) -> dict:
    sched = book.sched
    if sched.clock.demo:
        return {"configured": False, "reason": "disabled in demo mode"}
    if sched.sync is None:
        return {"configured": False, "reason": book.link_note or "paper keys not configured"}
    return {"configured": True, "endpoint": "paper-api.alpaca.markets", **sched.sync.status()}


def check_auth(header: str | None, password: str | None) -> bool:
    """HTTP Basic auth: any user name, the password from TRANCHE_PASSWORD.
    No password configured = open (local use)."""
    if not password:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[1]
    except Exception:
        return False
    return hmac.compare_digest(supplied.encode(), password.encode())


def make_handler(books: dict, provider_name: str, demo, backups=None):
    password = os.environ.get("TRANCHE_PASSWORD") or None
    ordered = list(books.values())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _route(self) -> tuple[Book | None, str]:
            """/api/<book>/<rest>; the old /api/<rest> means the hourly book."""
            m = re.fullmatch(r"/api/(1h|15m)(/.*)", self.path)
            if m:
                return books[m.group(1)], m.group(2)
            if self.path.startswith("/api/"):
                return books["1h"], self.path[4:]
            return None, self.path

        def _authorized(self) -> bool:
            if check_auth(self.headers.get("Authorization"), password):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Tranche dashboard"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return
            if self.path in ("/", "/index.html"):
                return self._send(200, (HERE / "static" / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            book, rest = self._route()
            if book and rest == "/state":
                st = build_state(book, ordered, provider_name, demo)
                st["backup"] = backups.status() if backups else None
                return self._send(200, st)
            if book and rest == "/download.db":
                with tempfile.TemporaryDirectory() as d:
                    path = os.path.join(d, "copy.db")
                    book.engine.store.backup_to(path)
                    data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="tranche_{book.key}_'
                                 f'{book.sched.clock.now():%Y-%m-%d_%H%M}.db"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            m = re.fullmatch(r"/bars/([A-Z][A-Z0-9.\-]{0,9})(\.csv)?", rest or "")
            if book and m:
                try:
                    rows = book.engine.bar_table(m.group(1), book.sched.clock.now())
                except Exception as e:
                    return self._send(502, {"error": redact(e)[:300]})
                if m.group(2):
                    return self._send(200, rows_csv(rows).encode(), "text/csv; charset=utf-8")
                return self._send(200, bars_page(book, m.group(1), rows).encode(),
                                  "text/html; charset=utf-8")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            if self.path == "/api/demo/pause" and demo:
                demo.paused = not demo.paused
                return self._send(200, {"ok": True})
            book, rest = self._route()
            if book is None:
                return self._send(404, {"error": "not found"})
            engine, sched = book.engine, book.sched
            now = sched.clock.now()
            try:
                body = self._body()
                if rest == "/settings":
                    was = engine.store.settings()["broker_sync_enabled"]
                    s = engine.update_settings(body)
                    if s["broker_sync_enabled"] and not was:
                        if sched.sync is None:
                            engine.update_settings({"broker_sync_enabled": False})
                            raise ValidationError(f"{book.label} book is not linked to an "
                                                  f"Alpaca paper account: {book.link_note}")
                        threading.Thread(target=sched.run_sync, daemon=True).start()
                elif rest == "/symbols":
                    added = engine.add_symbols(body.get("symbols", ""), body.get("mode"),
                                               body.get("risk_pct"), now, body.get("grade"))
                    threading.Thread(target=sched.run_tick, daemon=True).start()
                    return self._send(200, {"added": added})
                elif m := re.fullmatch(r"/symbols/(\d+)/grade", rest):
                    engine.set_grade(int(m.group(1)), body.get("grade"), now)
                    return self._send(200, {"ok": True})
                elif m := re.fullmatch(r"/symbols/(\d+)/(status|flatten)", rest):
                    if m.group(2) == "status":
                        engine.set_status(int(m.group(1)), body.get("status"), now)
                    else:
                        engine.flatten(int(m.group(1)), now)
                    threading.Thread(target=sched.run_sync, daemon=True).start()
                elif rest == "/tick":
                    sched.run_tick()
                elif rest == "/reset":
                    if backups:  # never wipe history without a copy first
                        try:
                            backups.run_backup("pre-reset", only=[book.key])
                        except Exception as e:
                            raise ValidationError(f"Reset cancelled - could not back up first: {e}")
                    engine.reset(now)
                    threading.Thread(target=sched.run_sync, daemon=True).start()
                elif rest == "/broker/sync":
                    if sched.sync is None:
                        raise ValidationError(f"{book.label} book is not linked to Alpaca paper")
                    sched.sync.sync(now)
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"ok": True})
            except (ValidationError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})

    return Handler


def rows_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


def bars_page(book: Book, symbol: str, rows: list[dict]) -> str:
    """Plain table of the bars the engine used, newest first, for checking
    against a chart. Signal rows are highlighted."""
    esc = html.escape
    cols = list(rows[0]) if rows else []
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join(
        "<tr%s>%s</tr>" % (' class="sig"' if r["signals"] else "",
                           "".join(f"<td>{esc('' if r[c] is None else str(r[c]))}</td>" for c in cols))
        for r in reversed(rows))
    return f"""<!doctype html><meta charset="utf-8"><title>{esc(symbol)} {esc(book.label)} bars</title>
<style>body{{font:13px system-ui,sans-serif;margin:16px;color:#16181d;background:#fff}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}th,td{{padding:4px 8px;
border-bottom:1px solid #e3e6eb;text-align:right;white-space:nowrap}}th{{color:#7a808c;font-weight:500;
position:sticky;top:0;background:#fff}}tr.sig{{background:#fff4d6}}td:nth-last-child(-n+2){{text-align:left}}
@media (prefers-color-scheme:dark){{body{{background:#0f1115;color:#e8eaee}}th{{background:#0f1115}}
th,td{{border-color:#2a2f38}}tr.sig{{background:#3a2f12}}}}</style>
<h2>{esc(symbol)} - {esc(book.label)} bars the engine used (newest first)</h2>
<p>Regular-hours bars built from 5-minute data; EMAs on bar closes; ATR (Wilder); session VWAP.
Highlighted rows fired a signal. <a href="{esc(symbol)}.csv">Download CSV</a></p>
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"""


def link_papers(demo: bool) -> dict[str, tuple]:
    """(AlpacaPaper | None, note) per book. The two books must never share a
    paper account: Alpaca nets one position per symbol per account, so two
    books trading the same symbol there would fight each other."""
    out = {}
    for key, _minutes, _label, prefix, _db in BOOKS:
        if demo:
            out[key] = (None, "disabled in demo mode")
            continue
        try:
            paper = AlpacaPaper.from_env(prefix)
        except BrokerError as e:
            raise SystemExit(str(e))
        note = None if paper else (f"set {prefix}_API_KEY_ID and {prefix}_API_SECRET_KEY "
                                   f"(paper keys) in .env")
        out[key] = (paper, note)
    k1, k15 = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_15M_API_KEY_ID")
    if out.get("15m", (None,))[0] and k1 and k1 == k15:
        out["15m"] = (None, "its keys are the same as the hourly book's; it needs a "
                            "separate Alpaca paper account (see SETUP.md)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--provider", default=None,
                    choices=["polygon", "fmp", "yahoo", "alpaca", "demo"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--demo", action="store_true",
                    help="synthetic prices + simulated clock (fresh demo databases each run)")
    ap.add_argument("--demo-days", type=int, default=15, help="weekdays of history to replay")
    ap.add_argument("--demo-speed", type=float, default=1.0,
                    help="seconds per simulated 15 minutes")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    args = ap.parse_args()
    url = f"http://{args.host}:{args.port}"

    # Claim the port first: a second copy must never start trading alongside the first.
    try:
        server = Server((args.host, args.port), BaseHTTPRequestHandler)
    except OSError:
        print(f"The dashboard is already running at {url} - opening it.")
        if args.open:
            webbrowser.open(url)
        return
    for note in load_dotenv(HERE / ".env"):
        print("  .env:", note)
    args.provider = (args.provider or os.environ.get("TRANCHE_PROVIDER", "").strip().lower()
                     or ("polygon" if os.environ.get("POLYGON_API_KEY") else
                         "fmp" if os.environ.get("FMP_API_KEY") else "yahoo"))
    if args.demo:
        args.provider = "demo"

    stores = {}
    for key, _m, _l, _p, db in BOOKS:
        path = HERE / (("demo_" + db) if args.demo else db)
        if args.demo and path.exists():
            path.unlink()
        stores[key] = Store(str(path))
    delay = stores["1h"].settings()["bar_close_delay_min"]
    clock = SimClock(args.demo_days, delay) if args.demo else RealClock()
    provider = CachedProvider(make_provider(
        args.provider, demo_origin=clock.now().date() - timedelta(days=60)))
    papers = link_papers(args.demo)

    books: dict[str, Book] = {}
    for key, minutes, label, _prefix, _db in BOOKS:
        store = stores[key]
        engine = Engine(store, provider, minutes)
        paper, note = papers[key]
        sync = BrokerSync(store, paper) if paper else None
        if sync is None and store.settings()["broker_sync_enabled"]:
            store.save_settings({"broker_sync_enabled": False})
        if args.demo:
            engine.add_symbols("DEMOA DEMOB", "long_short", 1.0, clock.now())
            engine.add_symbols("DEMOC", "short_only", 2.0, clock.now())
        books[key] = Book(key, minutes, label, engine, Scheduler(engine, clock, sync), note)

    if not args.demo:
        print("Checking connections...")
        for line in connection_check(
                provider, [(b.label, papers[k][0], papers[k][1]) for k, b in books.items()],
                clock.now()):
            print("  " + line)
    demo = None
    if args.demo:
        demo = DemoDriver(clock, [b.sched for b in books.values()], args.demo_speed)
        demo.start()
    else:
        for b in books.values():
            b.sched.start()
    backups = None
    if not args.demo:
        backups = Backups({k: b.engine.store for k, b in books.items()}, backup_folder(HERE), clock)
        try:
            written = backups.run_backup("startup")
            print(f"  backup: OK - {len(written)} files -> {backups.folder}")
        except Exception as e:
            print(f"  backup: FAILED - {e}")
        backups.start()
    server.RequestHandlerClass = make_handler(books, provider.name, demo, backups)
    linked = ", ".join(f"{b.label} {'linked' if b.sched.sync else 'not linked'}"
                       for b in books.values())
    print(f"Tranche dashboard on {url}  (provider={provider.name}"
          f"{', DEMO clock' if args.demo else ''}; alpaca paper: {linked})")
    if args.open:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
