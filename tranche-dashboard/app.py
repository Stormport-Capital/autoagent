"""Tranche dashboard: local web server + hourly scheduler.

    python app.py                      # live prices from Yahoo, http://127.0.0.1:8050
    python app.py --provider polygon   # Polygon data (POLYGON_API_KEY), recommended
    python app.py --provider alpaca    # Alpaca market data (keys via env)
    python app.py --demo               # synthetic prices on a fast simulated clock

Mock portfolio only: nothing here talks to a broker or places an order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from data import make_provider
from engine import SLEEVE_LABELS, Engine, ValidationError
from indicators import ET, RTH_OPEN, session_hour_ends
from store import Store

HERE = Path(__file__).parent


class RealClock:
    demo = False

    def now(self) -> datetime:
        return datetime.now(ET)


class SimClock:
    """Starts `days` weekdays ago and jumps one hourly bar per step."""

    demo = True

    def __init__(self, days: int, delay_min: int):
        d = datetime.now(ET).date()
        while days:
            d -= timedelta(days=1)
            days -= d.weekday() < 5
        self._now = datetime.combine(d, time(9, 0), tzinfo=ET)
        self.delay = timedelta(minutes=delay_min)
        self.lock = threading.Lock()

    def now(self) -> datetime:
        with self.lock:
            return self._now

    def step(self) -> datetime:
        with self.lock:
            nxt = next_boundary(self._now, self.delay)
            self._now = nxt
            return nxt


def boundaries(day: date, delay: timedelta) -> list[datetime]:
    return [e + delay for e in session_hour_ends(day)] if day.weekday() < 5 else []


def next_boundary(now: datetime, delay: timedelta) -> datetime:
    d = now.date()
    while True:
        for b in boundaries(d, delay):
            if b > now:
                return b
        d += timedelta(days=1)


class Scheduler(threading.Thread):
    """Runs a tick shortly after each RTH hourly bar closes (10:30 ... 16:00 ET)."""

    def __init__(self, engine: Engine, clock, demo_speed: float):
        super().__init__(daemon=True)
        self.engine, self.clock, self.demo_speed = engine, clock, demo_speed
        self.last_tick: datetime | None = None
        self.last_error: str | None = None
        self.paused = False

    def delay(self) -> timedelta:
        return timedelta(minutes=self.engine.store.settings()["bar_close_delay_min"])

    def run_tick(self) -> None:
        now = self.clock.now()
        try:
            self.engine.tick(now)
            self.last_error = None
        except Exception as e:  # keep the loop alive; surface on the dashboard
            self.last_error = str(e)
        self.last_tick = now

    def next_tick(self) -> datetime:
        return next_boundary(self.clock.now(), self.delay())

    def run(self) -> None:
        if self.clock.demo:
            while True:
                _time.sleep(self.demo_speed)
                if self.clock.now() >= datetime.now(ET):
                    self.paused = True  # replay has caught up with the present
                if not self.paused:
                    self.clock.step()
                    self.run_tick()
        self.run_tick()  # catch up on bars completed while the app was down
        while True:
            _time.sleep(20)
            now = self.clock.now()
            due = [b for b in boundaries(now.date(), self.delay()) if b <= now]
            if due and (self.last_tick is None or self.last_tick < due[-1]):
                self.run_tick()


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and RTH_OPEN <= now.time() < time(16, 0)


def build_state(engine: Engine, sched: Scheduler, provider_name: str) -> dict:
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
        "now": now.isoformat(),
        "demo": sched.clock.demo,
        "paused": sched.paused,
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
    }


def make_handler(engine: Engine, sched: Scheduler, provider_name: str):
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

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (HERE / "static" / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, build_state(engine, sched, provider_name))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            now = sched.clock.now()
            try:
                body = self._body()
                if self.path == "/api/settings":
                    engine.update_settings(body)
                elif self.path == "/api/symbols":
                    added = engine.add_symbols(body.get("symbols", ""), body.get("mode"),
                                               body.get("risk_pct"), now)
                    threading.Thread(target=sched.run_tick, daemon=True).start()
                    return self._send(200, {"added": added})
                elif m := re.fullmatch(r"/api/symbols/(\d+)/(status|flatten)", self.path):
                    if m.group(2) == "status":
                        engine.set_status(int(m.group(1)), body.get("status"), now)
                    else:
                        engine.flatten(int(m.group(1)), now)
                elif self.path == "/api/tick":
                    sched.run_tick()
                elif self.path == "/api/reset":
                    engine.reset(now)
                elif self.path == "/api/demo/pause" and sched.clock.demo:
                    sched.paused = not sched.paused
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"ok": True})
            except (ValidationError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--provider", default=os.environ.get("TRANCHE_PROVIDER", "yahoo"),
                    choices=["polygon", "yahoo", "alpaca", "demo"])
    ap.add_argument("--db", default=None, help="SQLite file (default tranche.db / demo.db)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--demo", action="store_true",
                    help="synthetic prices + simulated clock (fresh demo.db each run)")
    ap.add_argument("--demo-days", type=int, default=15, help="weekdays of history to replay")
    ap.add_argument("--demo-speed", type=float, default=2.0, help="seconds per simulated hour")
    args = ap.parse_args()

    if args.demo:
        args.provider = "demo"
        db = args.db or str(HERE / "demo.db")
        if os.path.exists(db):
            os.remove(db)
    else:
        db = args.db or str(HERE / "tranche.db")
    store = Store(db)
    delay = store.settings()["bar_close_delay_min"]
    clock = SimClock(args.demo_days, delay) if args.demo else RealClock()
    provider = make_provider(args.provider, demo_origin=clock.now().date() - timedelta(days=60))
    engine = Engine(store, provider)
    if args.demo:
        engine.add_symbols("DEMOA DEMOB", "long_short", 1.0, clock.now())
        engine.add_symbols("DEMOC", "short_only", 2.0, clock.now())

    sched = Scheduler(engine, clock, args.demo_speed)
    sched.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine, sched, provider.name))
    print(f"Tranche dashboard on http://{args.host}:{args.port}  "
          f"(provider={provider.name}, db={db}{', DEMO clock' if args.demo else ''})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
