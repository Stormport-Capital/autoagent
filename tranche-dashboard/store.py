"""SQLite persistence for settings, symbols, tranches, events and equity."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

DEFAULT_SETTINGS = {
    "starting_capital": 100_000.0,
    "borrow_rate_pct": 10.0,       # annual hard-to-borrow fee on short notional
    "borrow_day_count": 360,       # broker convention: rate / 360 per calendar night
    "stop_atr_mult": 2.0,          # EMA sizing fallback (x ATR) when < 5 past crosses; NOT a stop
    "atr_period": 14,
    "max_leverage": 2.0,           # gross exposure cap as a multiple of equity
    "slippage_bps": 5.0,           # adverse fill vs bar close / stop, each side
    "bar_close_delay_min": 2,      # wait this long after an hourly bar closes
    "broker_sync_enabled": False,  # mirror the model into the Alpaca PAPER account
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('short_only', 'long_short')),
    risk_pct REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active | paused | removed
    added_at TEXT NOT NULL,
    last_bar_end TEXT,
    last_price REAL,
    snapshot TEXT,                          -- JSON: latest indicator readings
    error TEXT,
    grade TEXT                              -- A+ / A / B / C, or NULL = custom risk %
);
CREATE TABLE IF NOT EXISTS tranches (
    id INTEGER PRIMARY KEY,
    symbol_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sleeve TEXT NOT NULL,                   -- EMA5_10 | EMA10_20 | VWAP
    side TEXT NOT NULL,                     -- long | short
    qty INTEGER NOT NULL,
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL,                      -- VWAP tranche: daily 10-MA
    risk_dollars REAL NOT NULL,
    fee_through TEXT NOT NULL,              -- last date borrow fee was charged
    borrow_fees REAL NOT NULL DEFAULT 0,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl REAL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT,
    sleeve TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    reason TEXT,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    filled_qty REAL,
    filled_avg_price REAL,
    filled_at TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS broker_equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tranches)")}
        if "target_price" not in cols:  # databases created before the Russo exits
            self.db.execute("ALTER TABLE tranches ADD COLUMN target_price REAL")
        scols = {r[1] for r in self.db.execute("PRAGMA table_info(symbols)")}
        if "grade" not in scols:  # databases created before trade grades
            self.db.execute("ALTER TABLE symbols ADD COLUMN grade TEXT")
        self.db.commit()

    # -- generic helpers -------------------------------------------------
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # -- settings --------------------------------------------------------
    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        for r in self.q("SELECT key, value FROM settings"):
            if r["key"] in s:
                s[r["key"]] = json.loads(r["value"])
        return s

    def save_settings(self, updates: dict) -> None:
        for k, v in updates.items():
            if k in DEFAULT_SETTINGS:
                self.x("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(v)))

    # -- events / equity -------------------------------------------------
    def log(self, ts: datetime, kind: str, message: str, symbol=None, sleeve=None) -> None:
        self.x("INSERT INTO events (ts, symbol, sleeve, kind, message) VALUES (?,?,?,?,?)",
               (ts.isoformat(), symbol, sleeve, kind, message))

    def record_equity(self, ts: datetime, equity: float) -> None:
        self.x("INSERT OR REPLACE INTO equity VALUES (?, ?)", (ts.isoformat(), equity))

    def backup_to(self, path: str) -> None:
        """Consistent copy of the live database (SQLite online backup), written
        under a temporary name and renamed when complete."""
        tmp = path + ".partial"
        with self.lock:
            dest = sqlite3.connect(tmp)
            try:
                self.db.backup(dest)
            finally:
                dest.close()
        os.replace(tmp, path)

    def reset_portfolio(self) -> None:
        with self.lock:
            for t in ("tranches", "events", "equity"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute("UPDATE symbols SET last_bar_end = NULL")
            self.db.commit()
