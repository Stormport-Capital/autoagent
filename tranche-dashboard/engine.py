"""Strategy + mock-portfolio engine.

Each watched symbol has three independent sleeves ("tranches"), each worth one
third of the symbol's risk budget:

  EMA5_10   5 EMA crosses below 10 EMA (hourly)  -> short
            5 EMA crosses above 10 EMA           -> cover; long too if long_short
  EMA10_20  same rule with the 10 and 20 EMA
  VWAP      Russo "VWAP fail" (Trigger B), rising edge: an earlier hourly bar
            this session closed above VWAP, this bar closes below VWAP and
            prints a lower high than the session high so far -> short
            (always short-only). Russo exits, stop checked before target:
              stop   = max(session high of day incl. this bar, entry); fires on
                       the first bar after entry that prints a new high of day
                       (or trades back through entry on a later session), and
                       fills at that level - the worst price in the bar
              target = daily 10-day SMA of prior sessions' closes; covers when
                       an hourly low touches it, filled at the target (or the
                       open if the bar is already below it). No setup or entry
                       filter: a short opened at/below its 10-MA covers at the
                       next bar's open.
              held overnight; no end-of-session flatten.

EMA tranches have NO stop: they enter and exit only on crossovers (the
opposite cross closes the position, and reverses it in long_short mode).
Crosses are judged at the chart's price tick, so equal EMAs never signal.
Sizing ("how much a losing trade typically gives back"): risk per share =
the 75th-percentile adverse close-to-close move of this stock's own past
cross-to-cross trades for that EMA pair (floored at 1 ATR); with fewer than 5
past crosses it falls back to stop_atr_mult x ATR. It is not an exit.
Sizing: qty = (equity x risk_pct / 3) / stop distance, capped so gross exposure
stays <= equity x max_leverage. Fills are simulated at the hourly close (entries,
signal exits) or at the stop / gapped open (stops), with slippage_bps adverse.
Short tranches pay the symbol's borrow rate (its own, else the book's borrow_rate_pct)
/ borrow_day_count per calendar night held; same-day shorts pay none. A short whose
rate is above overnight_borrow_max_pct is flat by 15:55 like a close-by-end-of-day name.
Only hourly bars that complete after the symbol was added are ever traded.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
import re
from datetime import date, datetime, time, timedelta

from indicators import (ET, RTH_OPEN, Bar, atr, cross_adverse_moves, ema_cross, ema_side, percentile,
                        price_tick,
                        daily_closes, daily_sma, ema, in_rth, is_final_bar,
                        resample, session_bar_ends, session_vwap)
from data import redact
from store import DEFAULT_SETTINGS, Store

SLEEVES = ("EMA5_10", "EMA10_20", "VWAP")
SLEEVE_LABELS = {
    "EMA5_10": "5/10 EMA cross",
    "EMA10_20": "10/20 EMA cross",
    "VWAP": "VWAP fail (Russo)",
}
RISK_MIN, RISK_MAX = 0.5, 3.0
# the only tranche the IBKR account may follow (short only, flat by 15:55, fixed stop)
LIVE_SLEEVES = ("EMA5_10",)
# live 5/10 short: the state entry is judged on the bar ending 9:45; the opening
# range is the regular-hours 5-min bars ending by then; at most this many entries a day
LIVE_STATE_BAR_END = time(9, 45)
LIVE_MAX_ENTRIES_PER_DAY = 2
LIVE_TRIGGERS = ("state_0945", "cross")
# trade grade -> risk % of equity for the symbol (split across its 3 tranches)
GRADES = {"A+": 3.0, "A": 2.0, "B": 1.5, "C": 1.0}
# EMA sizing: risk per share = this percentile of past cross-to-cross adverse moves
EMA_RISK_PCTL = 0.75
EMA_MIN_SAMPLES = 5
STALE_WAIT = timedelta(minutes=20)
# "Close by end of day" symbols: no new entries from this time, and every open
# tranche is covered/sold at the close of the 5-minute bar ending here.
EOD_CUTOFF = time(15, 55)


def eod_cutoff(day: date) -> datetime:
    return datetime.combine(day, EOD_CUTOFF, tzinfo=ET)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

SETTING_BOUNDS = {
    "starting_capital": (1_000.0, 1e9),
    "borrow_rate_pct": (0.0, 2000.0),
    "overnight_borrow_max_pct": (0.0, 2000.0),
    "borrow_day_count": (360, 365),
    "stop_atr_mult": (0.25, 10.0),   # EMA sizing unit (x ATR); not a stop
    "atr_period": (2, 50),
    "max_leverage": (0.1, 10.0),
    "slippage_bps": (0.0, 200.0),
    "bar_close_delay_min": (0, 30),
    "vwap_max_sessions": (0, 30),
    "vwap_scale_out_20": (0, 1),
}


class ValidationError(ValueError):
    pass


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass(frozen=True)
class Fill:
    """Where signals from a bar execute: at its close for intraday bars; at the
    next session's 9:30 open for the 15:00-16:00 bar."""
    price: float
    when: datetime
    session: date
    how: str = ""  # human-readable: which price this is and where it came from


def opening_fade(bars: list[Bar], five: list[Bar], i: int) -> bool:
    """Opening fade, the VWAP fail for a stock that never trades above VWAP:
    bar i is the session's 2nd bar; bar 1 was red and closed below VWAP, and
    bar 2 is red, closes below VWAP and prints a lower high than bar 1.
    Only the first two bars of a session can trigger it."""
    b = bars[i]
    earlier = [x for x in bars[:i] if x.session == b.session]
    if len(earlier) != 1:
        return False
    b1 = earlier[0]
    v1 = session_vwap(five, b.session, b1.end)
    v2 = session_vwap(five, b.session, b.end)
    if v1 is None or v2 is None:
        return False
    return (b1.close < b1.open and b1.close < v1
            and b.close < b.open and b.close < v2 and b.high < b1.high)


def vwap_fail(bars: list[Bar], five: list[Bar], i: int) -> bool:
    """Russo Trigger B ("VWAP fail") evaluated on hourly bar i:

        above_vwap_earlier AND close < vwap AND high < hod

    above_vwap_earlier: an earlier hourly bar of the same session closed above
    its own session VWAP. hod: the session high of the bars before bar i, so
    the current bar must print a lower high. No red-candle test, as in the
    Russo code: a green bar that closes under VWAP with a lower high qualifies.
    """
    b = bars[i]
    earlier = [x for x in bars[:i] if x.session == b.session]
    if not earlier:
        return False
    vwap = session_vwap(five, b.session, b.end)
    if vwap is None or b.close >= vwap:
        return False
    if b.high >= max(x.high for x in earlier):
        return False
    for x in earlier:
        v = session_vwap(five, b.session, x.end)
        if v is not None and x.close > v:
            return True
    return False


class Engine:
    def __init__(self, store: Store, provider, minutes: int = 60):
        self.store = store
        self.provider = provider
        self.minutes = minutes  # bar size: 60 (hourly) or 15

    # ------------------------------------------------------------------ setup
    def update_settings(self, updates: dict) -> dict:
        clean = {}
        for k, v in updates.items():
            if k not in DEFAULT_SETTINGS:
                raise ValidationError(f"unknown setting {k}")
            if k == "broker_sync_enabled":
                clean[k] = v is True or v == "true"
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValidationError(f"{k} must be a number")
            lo, hi = SETTING_BOUNDS[k]
            if not lo <= num <= hi:
                raise ValidationError(f"{k} must be between {lo:g} and {hi:g}")
            if k == "borrow_day_count" and num not in (360, 365):
                raise ValidationError("borrow_day_count must be 360 or 365")
            clean[k] = int(num) if isinstance(DEFAULT_SETTINGS[k], int) else num
        self.store.save_settings(clean)
        return self.store.settings()

    def add_symbols(self, raw: str, mode: str, risk_pct, now: datetime,
                    grade: str | None = None, eod_close: bool = False) -> list[str]:
        if mode not in ("short_only", "long_short"):
            raise ValidationError("mode must be short_only or long_short")
        if grade in GRADES:
            risk_pct = GRADES[grade]
        elif grade not in (None, "", "custom"):
            raise ValidationError(f"grade must be one of {', '.join(GRADES)} or custom")
        try:
            risk = float(risk_pct)
        except (TypeError, ValueError):
            raise ValidationError("risk % must be a number")
        if not RISK_MIN <= risk <= RISK_MAX:
            raise ValidationError(f"risk % must be between {RISK_MIN} and {RISK_MAX}")
        added = []
        for sym in re.split(r"[\s,;]+", (raw or "").upper()):
            if not sym:
                continue
            if not SYMBOL_RE.match(sym):
                raise ValidationError(f"invalid symbol {sym!r}")
            if self.store.q("SELECT 1 FROM symbols WHERE symbol=? AND status!='removed'", (sym,)):
                continue
            self.store.x(
                "INSERT INTO symbols (symbol, mode, risk_pct, added_at, grade, eod_close) "
                "VALUES (?,?,?,?,?,?)",
                (sym, mode, risk, now.isoformat(), grade if grade in GRADES else None,
                 1 if eod_close else 0))
            g = f"grade {grade}, " if grade in GRADES else ""
            e = ", close by end of day" if eod_close else ""
            self.store.log(now, "added", f"watching {sym}: {mode.replace('_', ' ')}, "
                           f"{g}risk {risk:g}% of equity{e}", sym)
            added.append(sym)
        if not added:
            raise ValidationError("no new symbols to add")
        return added

    def set_grade(self, symbol_id: int, grade: str, now: datetime) -> None:
        """Change a symbol's grade (and so its risk %). Applies to new entries only."""
        if grade not in GRADES:
            raise ValidationError(f"grade must be one of {', '.join(GRADES)}")
        sym = self._symbol(symbol_id)
        self.store.x("UPDATE symbols SET grade=?, risk_pct=? WHERE id=?",
                     (grade, GRADES[grade], symbol_id))
        self.store.log(now, "grade", f"{sym['symbol']} grade {grade} -> risk "
                       f"{GRADES[grade]:g}% (new entries)", sym["symbol"])

    def set_borrow(self, symbol_id: int, pct, now: datetime) -> None:
        """This symbol's annual borrow fee (%), or None for the book default.
        Applies to borrow charged from the next night on."""
        sym = self._symbol(symbol_id)
        if pct in (None, ""):
            val = None
        else:
            try:
                val = float(pct)
            except (TypeError, ValueError):
                raise ValidationError("borrow rate must be a number (% per year)")
            if not 0 <= val <= 2000:
                raise ValidationError("borrow rate must be between 0 and 2000 % per year")
        self.store.x("UPDATE symbols SET borrow_pct=? WHERE id=?", (val, symbol_id))
        s = self.store.settings()
        txt = f"{val:g}%/yr" if val is not None else f"book default ({s['borrow_rate_pct']:g}%/yr)"
        lim = s["overnight_borrow_max_pct"]
        note = (f"; above the {lim:g}% overnight limit, so its shorts are flat by "
                f"{EOD_CUTOFF:%H:%M}" if lim and val is not None and val > lim else "")
        self.store.log(now, "borrow", f"{sym['symbol']} borrow rate {txt}{note}", sym["symbol"])

    def borrow_pct(self, sym: dict, s: dict) -> float:
        v = sym.get("borrow_pct")
        return float(v) if v is not None else float(s["borrow_rate_pct"])

    def day_only(self, sym: dict, side: str, s: dict) -> str | None:
        """Why a position on this side must be flat by 15:55 (None = may hold overnight)."""
        if sym.get("eod_close"):
            return "close by end of day"
        if sym.get("live"):
            return "close by end of day (IBKR live symbol)"
        # Only a rate entered for this symbol can force it flat: the book default is
        # a cost estimate, not an observed fee, so it never changes how a trade is held.
        lim = float(s.get("overnight_borrow_max_pct") or 0)
        if side == "short" and lim and sym.get("borrow_pct") is not None \
                and float(sym["borrow_pct"]) > lim:
            return (f"borrow {self.borrow_pct(sym, s):g}%/yr is above the {lim:g}% overnight "
                    f"limit")
        return None

    def set_live(self, symbol_id: int, sleeves, now: datetime) -> list[str]:
        """Which of this symbol's tranches the IBKR account follows (empty =
        none). Only LIVE_SLEEVES may be chosen. A live symbol is flat by 15:55
        (every tranche, whatever its own EOD switch) and its 5/10 short has a
        5-minute high-of-day stop; see day_only and _live_stop."""
        sym = self._symbol(symbol_id)
        asked = set(sleeves or [])
        if asked - set(SLEEVE_LABELS):
            raise ValidationError("unknown tranche")
        if asked - set(LIVE_SLEEVES):
            raise ValidationError("only the 5/10 EMA tranche can trade live; the VWAP-fail "
                                  "and 10/20 tranches are refused")
        sleeves = [s for s in SLEEVE_LABELS if s in asked]
        if sleeves and sym["status"] == "removed":
            raise ValidationError(f"{sym['symbol']} was removed")
        self.store.x("UPDATE symbols SET live=? WHERE id=?", (",".join(sleeves) or None, symbol_id))
        self.store.log(now, "live", f"{sym['symbol']}: IBKR follows " + (
            " + ".join(SLEEVE_LABELS[s] for s in sleeves) if sleeves else "nothing (live off)"),
            sym["symbol"])
        return sleeves

    def set_eod_close(self, symbol_id: int, on: bool, now: datetime) -> None:
        """Day-trade mode: the symbol still trades all session, but takes no new
        entries from 15:55 ET and is flat at the 15:55 5-minute close."""
        sym = self._symbol(symbol_id)
        if sym["status"] == "removed":
            raise ValidationError(f"{sym['symbol']} was removed")
        self.store.x("UPDATE symbols SET eod_close=? WHERE id=?", (1 if on else 0, symbol_id))
        self.store.log(now, "eod", f"{sym['symbol']}: close by end of day "
                       f"{'ON - flat by the 15:55 close every session' if on else 'OFF - positions may be held overnight'}",
                       sym["symbol"])

    def rename_symbol(self, symbol_id: int, new: str, now: datetime, ratio=1.0) -> dict:
        """Ticker change (e.g. AIXC -> FFR): the watchlist entry and its OPEN
        tranches move to the new ticker; closed trades keep the old one.
        `ratio` > 1 is a reverse split N-for-1: shares / N, prices x N."""
        new = (new or "").strip().upper()
        if not SYMBOL_RE.match(new):
            raise ValidationError(f"invalid symbol {new!r}")
        try:
            ratio = float(ratio or 1)
        except (TypeError, ValueError):
            raise ValidationError("split ratio must be a number")
        if not 0.001 <= ratio <= 10000:
            raise ValidationError("split ratio must be between 0.001 and 10000")
        sym = self._symbol(symbol_id)
        old = sym["symbol"]
        if sym["status"] == "removed":
            raise ValidationError(f"{old} was removed")
        if new == old:
            raise ValidationError("that's already its ticker")
        if self.store.q("SELECT 1 FROM symbols WHERE symbol=? AND status!='removed'", (new,)):
            raise ValidationError(f"{new} is already on this book's watchlist - remove it first")
        with self.store.lock:
            moved = []
            for t in self._open(symbol_id):
                qty = max(1, round(t["qty"] / ratio))
                scale = lambda v: None if v is None else v * ratio
                self.store.x("""UPDATE tranches SET symbol=?, qty=?, entry_price=?, stop_price=?,
                                target_price=?, stop_dist=? WHERE id=?""",
                             (new, qty, scale(t["entry_price"]), scale(t["stop_price"]),
                              scale(t["target_price"]), scale(t.get("stop_dist")), t["id"]))
                moved.append(f"{SLEEVE_LABELS[t['sleeve']]} {t['side']} {t['qty']}->{qty}")
            self.store.x("""UPDATE symbols SET symbol=?, renamed_from=?, snapshot=NULL, error=NULL,
                            last_price=CASE WHEN last_price IS NULL THEN NULL ELSE last_price*? END
                            WHERE id=?""", (new, old, ratio, symbol_id))
            split = f", reverse split {ratio:g}-for-1" if ratio != 1 else ""
            self.store.log(now, "renamed", f"{old} is now {new}{split}; open tranches moved: "
                           f"{'; '.join(moved) or 'none'}", new)
        return {"old": old, "new": new, "moved": len(moved)}

    def set_status(self, symbol_id: int, status: str, now: datetime) -> None:
        if status not in ("active", "paused", "removed"):
            raise ValidationError("bad status")
        sym = self._symbol(symbol_id)
        with self.store.lock:
            if status == "removed":
                self.flatten(symbol_id, now, "symbol removed")
            self.store.x("UPDATE symbols SET status=? WHERE id=?", (status, symbol_id))
            self.store.log(now, status, f"{sym['symbol']} {status}", sym["symbol"])

    def flatten(self, symbol_id: int, now: datetime, reason="manual flatten") -> None:
        sym = self._symbol(symbol_id)
        s = self.store.settings()
        with self.store.lock:
            for t in self._open(symbol_id):
                if sym["last_price"] is None:
                    raise ValidationError("no price yet to flatten at")
                self._accrue_borrow(t, now.astimezone(ET).date(), sym["last_price"], s)
                self._close(t, sym["last_price"], now, reason, s)

    def reset(self, now: datetime) -> None:
        """Wipe trades and history; keep symbols, which restart from `now`."""
        with self.store.lock:
            self.store.reset_portfolio()
            self.store.x("UPDATE symbols SET added_at=? WHERE status!='removed'",
                         (now.isoformat(),))
            self.store.log(now, "reset", "portfolio reset")

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime, only_behind: tuple | None = None) -> None:
        """only_behind=(bar_end, acts_at): a retry - process only the symbols
        still missing that bar, so one lagging symbol doesn't re-fetch them all."""
        with self.store.lock:
            s = self.store.settings()
            for sym in self.store.q("SELECT * FROM symbols WHERE status!='removed' ORDER BY id"):
                if only_behind and not self._is_behind(sym, *only_behind):
                    continue
                try:
                    self._process_symbol(sym, now, s)
                except Exception as e:  # one bad symbol must not stop the rest
                    msg = redact(e)[:300]
                    self.store.x("UPDATE symbols SET error=? WHERE id=?", (msg, sym["id"]))
                    self.store.log(now, "error", msg, sym["symbol"])
            self.store.record_equity(now, self.equity(s))

    def behind(self, bar_end: datetime, acts_at: datetime | None = None) -> bool:
        """True if any watched symbol has not yet processed the hourly bar
        ending at `bar_end` (e.g. a delayed feed had not printed it yet).
        `acts_at` is when that bar's signals execute (the next open for the
        final bar); symbols added after that don't need it."""
        return any(self._is_behind(r, bar_end, acts_at) for r in self.store.q(
            "SELECT added_at, last_bar_end FROM symbols WHERE status!='removed'"))

    @staticmethod
    def _is_behind(r: dict, bar_end: datetime, acts_at: datetime | None = None) -> bool:
        if _dt(r["added_at"]) >= (acts_at or bar_end):
            return False
        return r["last_bar_end"] is None or _dt(r["last_bar_end"]) < bar_end

    def _load(self, symbol: str, now: datetime, s: dict):
        """5-minute data -> completed bars of this book's size + indicators."""
        five = self.provider.five_min_bars(symbol, now)
        delay = timedelta(minutes=s["bar_close_delay_min"])
        # an hour counts as complete once the feed has printed through its end
        # (delayed feeds lag), or 20 min later for names with no late prints
        data_end = max((b.end for b in five), default=None)
        bars = [b for b in resample(five, self.minutes)
                if b.end + delay <= now
                and ((data_end and data_end >= b.end) or b.end + max(delay, STALE_WAIT) <= now)]
        if not bars:
            raise RuntimeError("no completed bars returned")
        closes = [b.close for b in bars]
        ind = {
            "e5": ema(closes, 5), "e10": ema(closes, 10), "e20": ema(closes, 20),
            "atr": atr(bars, int(s["atr_period"])),
            "daily": daily_closes(five),
        }
        return five, bars, ind

    def bar_table(self, symbol: str, now: datetime, n: int = 80) -> list[dict]:
        """The last n completed bars exactly as the engine sees them, with the
        indicators and which signals fired - for checking against a chart."""
        s = self.store.settings()
        five, bars, ind = self._load(symbol, now, s)
        rows = []
        for i in range(max(0, len(bars) - n), len(bars)):
            b, j = bars[i], i - 1
            e5, e10, e20 = ind["e5"], ind["e10"], ind["e20"]
            sig = []
            if i > 0:
                tick = price_tick(b.close)
                for name, x, y in (("5/10", e5, e10), ("10/20", e10, e20)):
                    c = ema_cross(x, y, i, tick)
                    if c:
                        sig.append(f"{name} cross {'UP' if c > 0 else 'DOWN'}")
                if vwap_fail(bars, five, i) and not vwap_fail(bars, five, j):
                    sig.append("VWAP fail" + (" (last bar: not carried to the open)"
                                              if is_final_bar(b) else ""))
            vw = session_vwap(five, b.session, b.end)
            r = lambda v: None if v is None else round(v, 4)
            rows.append({
                "bar_start_et": b.start.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                "bar_end_et": b.end.astimezone(ET).strftime("%H:%M"),
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": int(b.volume), "ema5": r(e5[i]), "ema10": r(e10[i]),
                "ema20": r(e20[i]), "atr": r(ind["atr"][i]), "vwap": r(vw),
                "signals": "; ".join(sig),
                "acts_at": ("next session 9:30 open (EMA only)" if is_final_bar(b) else
                            b.end.astimezone(ET).strftime("%H:%M")) if sig else "",
            })
        return rows

    def _process_symbol(self, sym: dict, now: datetime, s: dict) -> None:
        five, bars, ind = self._load(sym["symbol"], now, s)
        added = _dt(sym["added_at"])
        last = _dt(sym["last_bar_end"])
        for i, b in enumerate(bars):
            if last and b.end <= last:
                continue
            if is_final_bar(b):
                # the 15:00-16:00 bar acts at the next session's 9:30 open
                fill = self._next_open(sym["symbol"], b, five, now)
                if fill is None:
                    break  # before the next open: wait for the 9:30 check
            else:
                fill = Fill(b.close, b.end, b.session, f"at the bar close {b.close:.4g}")
            self._eod_sweep(sym, five, s, before=b.end)  # an outage spanned a 15:55
            if fill.when > added:
                self._process_bar(sym, bars, five, i, ind, s, fill)
            last = b.end
            self.store.x("UPDATE symbols SET last_bar_end=?, last_price=? WHERE id=?",
                         (last.isoformat(), fill.price, sym["id"]))
        self._eod_sweep(sym, five, s, now=now)
        # live readings for the dashboard (latest 5-minute print, today's VWAP)
        latest = five[-1] if five else bars[-1]
        vwap_now = session_vwap(five, latest.session, latest.end)
        n = len(bars) - 1
        snap = {
            "bar_end": bars[-1].end.isoformat(),
            "hourly_close": bars[-1].close,
            "ema5": ind["e5"][n], "ema10": ind["e10"][n], "ema20": ind["e20"][n],
            "atr": ind["atr"][n], "vwap": vwap_now, "price_time": latest.end.isoformat(),
        }
        self.store.x("UPDATE symbols SET last_price=?, snapshot=?, error=NULL WHERE id=?",
                     (latest.close, json.dumps(snap), sym["id"]))

    def _eod_sweep(self, sym: dict, five: list[Bar], s: dict, *, before: datetime | None = None,
                   now: datetime | None = None) -> None:
        """Close-by-end-of-day: cover/sell every open tranche at the close of the
        5-minute bar ending 15:55 on the day it was opened. Runs before a bar
        that ends after that cutoff (`before`), or once the cutoff has passed
        on the clock (`now`, after the bar-close delay; a late feed gets the
        usual grace period, then the latest print up to 15:55 is used)."""
        delay = timedelta(minutes=s["bar_close_delay_min"])
        for t in self._open(sym["id"]):
            why = self.day_only(sym, t["side"], s)
            if not why:
                continue
            opened = _dt(t["entry_time"])
            day = opened.astimezone(ET).date()
            cut = eod_cutoff(day)
            if before is not None and not cut < before:
                continue
            day_bars = [x for x in five if x.session == day and x.end <= cut]
            if now is not None:
                if now < cut + delay:
                    continue
                if not any(x.end >= cut for x in day_bars) and now < cut + max(delay, STALE_WAIT):
                    continue  # the 15:50-15:55 bar hasn't printed yet: look again shortly
            x = next((x for x in reversed(day_bars) if x.end > opened), None) or (
                day_bars[-1] if day_bars else None)
            if x is None:
                continue
            self._close(t, x.close, max(x.end, opened),
                        f"{why}: flat at the {x.end.astimezone(ET):%b %d %H:%M} "
                        f"5-min close {x.close:.4g}", s)

    def _next_open(self, symbol: str, b: Bar, five: list[Bar], now: datetime) -> Fill | None:
        """The opening print of the session after bar b, once it exists."""
        for x in five:
            if x.session > b.session and in_rth(x.start):
                return Fill(x.open, x.start, x.session,
                            f"at the {x.session:%b %d} 9:30 open {x.open:.4g} (first 5-min bar)")
        today = now.astimezone(ET)
        if today.date() <= b.session or today.weekday() >= 5 or today.time() < RTH_OPEN:
            return None
        getter = getattr(self.provider, "session_open", None)
        price = getter(symbol, today.date(), now) if getter else None
        if price is None:
            return None
        return Fill(price, datetime.combine(today.date(), RTH_OPEN, tzinfo=ET), today.date(),
                    f"at the {today:%b %d} 9:30 open {price:.4g} ({self.provider.name} first minute)")

    def _process_bar(self, sym, bars: list[Bar], five: list[Bar], i: int, ind: dict, s: dict,
                     fill: Fill | None = None):
        b = bars[i]
        fill = fill or Fill(b.close, b.end, b.session, f"at the bar close {b.close:.4g}")
        bar_txt = (f"{b.start.astimezone(ET):%b %d %H:%M}-{b.end.astimezone(ET):%H:%M} bar "
                   f"O{b.open:.4g} H{b.high:.4g} L{b.low:.4g} C{b.close:.4g}")
        prev = bars[i - 1] if i > 0 else None
        name = sym["symbol"]
        open_tr = {t["sleeve"]: t for t in self._open(sym["id"])}

        # 1. borrow fees for shorts carried overnight, marked at the prior close
        if prev is not None:
            for t in open_tr.values():
                self._accrue_borrow(t, b.session, prev.close, s)

        # 2. exits against this bar's range, stop before target
        hod = max(x.high for x in bars[:i + 1] if x.session == b.session)  # incl. this bar
        ma10 = daily_sma(ind["daily"], b.session, 10)
        ma20 = daily_sma(ind["daily"], b.session, 20)
        for sleeve, t in list(open_tr.items()):
            if b.end <= _dt(t["entry_time"]):
                continue
            if sleeve == "VWAP":  # Russo: high-of-day stop trailed daily, 10-/20-MA targets
                stop = self._vwap_stop(t, bars, i)
                if b.high >= stop:
                    gap = b.open > stop  # a stop order fills at the open when it gaps through
                    px = b.open if gap else stop
                    self._close(t, px, b.end, f"stop {stop:.4g} (entry-day high, trailed to the "
                                f"lowest session high since) {'gapped through: filled at the open' if gap else 'hit'}; "
                                f"{bar_txt}", s)
                    del open_tr[sleeve]
                    continue
                t = self._vwap_targets(t, b, ma10, ma20, bar_txt, s)
                if t is not None:
                    t = self._vwap_time_stop(t, bars, i, fill, s)
                if t is None:
                    del open_tr[sleeve]
                else:
                    open_tr[sleeve] = t
                    self.store.x("UPDATE tranches SET stop_price=? WHERE id=?", (stop, t["id"]))
                continue
            # EMA tranches have no stop: they only exit on the opposite cross (step 3),
            # except a live 5/10 short, which also stops on a 5-min close above its fixed stop
            if t["side"] == "short" and t.get("trigger") in LIVE_TRIGGERS:
                if self._live_stop(t, five, b, s):
                    del open_tr[sleeve]

        # held into the next session (final bar): charge that night's borrow
        if fill.session != b.session:
            for t in open_tr.values():
                self._accrue_borrow(t, fill.session, b.close, s)

        # 3. signals at the hourly close, executed at `fill`
        e5, e10, e20, a = ind["e5"], ind["e10"], ind["e20"], ind["atr"][i]
        j = i - 1
        ema_ready = i > 0 and e20[j] is not None and a is not None
        if not ema_ready:
            self.store.log(b.end, "warmup", "not enough bar history for EMA20/ATR yet", name)
        else:
            ctx = (f"on the {bar_txt}; EMA5 {e5[j]:.4g}->{e5[i]:.4g}, EMA10 {e10[j]:.4g}->"
                   f"{e10[i]:.4g}, EMA20 {e20[j]:.4g}->{e20[i]:.4g}; filled {fill.how}")
            tick = price_tick(b.close)
            c1, c2 = ema_cross(e5, e10, i, tick), ema_cross(e10, e20, i, tick)
            u2 = self._ema_unit(bars, e10, e20, i, a, s) if c2 else None
            if "EMA5_10" in (sym.get("live") or ""):
                self._live_510(sym, open_tr.get("EMA5_10"), fill, bars, five, i, e5, e10, s, ctx,
                               lambda: self._ema_unit(bars, e5, e10, i, a, s))
            else:
                u1 = self._ema_unit(bars, e5, e10, i, a, s) if c1 else None
                self._ema_sleeve(sym, "EMA5_10", open_tr.get("EMA5_10"), fill, u1, s,
                                 c1 < 0, c1 > 0, ctx)
            self._ema_sleeve(sym, "EMA10_20", open_tr.get("EMA10_20"), fill, u2, s,
                             c2 < 0, c2 > 0, ctx)

        vwap = session_vwap(five, b.session, b.end)
        if vwap is None:
            return
        # Russo Trigger B, rising edge only: fires on the bar where it turns true;
        # or the opening fade (bars 1-2 red below VWAP, never above it yet)
        fade = opening_fade(bars, five, i)
        lost = fade or (vwap_fail(bars, five, i) and not (i > 0 and vwap_fail(bars, five, i - 1)))
        if lost and fill.session != b.session:
            # VWAP resets every session: a fail on the last bar is not carried to the open
            self.store.log(b.end, "skip", f"VWAP fail on the last bar of the day ({bar_txt}) - "
                           "VWAP resets overnight, so it is not traded at the next open",
                           name, "VWAP")
        elif lost and "VWAP" not in open_tr:
            at_open = " (bar closed at 16:00: entered at the next open)" if fill.session != b.session else ""
            kind = ("VWAP fail (opening fade): bars 1 and 2 red, both closed below VWAP"
                    if fade else "VWAP fail")
            self._enter(sym, "VWAP", "short", fill, None, s,
                        f"{kind}: close {b.close:.2f} < VWAP {vwap:.2f}, "
                        f"high {b.high:.2f} below HOD {hod:.2f}{at_open}; {bar_txt}; "
                        f"filled {fill.how}",
                        stop_level=hod, target=daily_sma(ind["daily"], fill.session, 10),
                        trigger="open_fade" if fade else "fail")

    def _live_stop(self, t: dict, five: list[Bar], b: Bar, s) -> bool:
        """Live 5/10 short hard stop: the first regular-hours 5-minute bar after
        entry, up to the end of book bar b, whose CLOSE is above the tranche's
        stop_price, which is fixed at entry and never moves. Covers at that close."""
        opened, stop = _dt(t["entry_time"]), t["stop_price"]
        for x in five:
            if x.end <= opened or x.end <= b.start or x.end > b.end or not in_rth(x.start):
                continue
            if x.close > stop:
                self._close(t, x.close, x.end,
                            f"stop: 5-min close {x.close:.4g} above the fixed stop {stop:.4g} "
                            f"({x.start.astimezone(ET):%b %d %H:%M}-{x.end.astimezone(ET):%H:%M} bar)", s)
                return True
        return False

    def _live_510(self, sym, t, f: "Fill", bars: list[Bar], five: list[Bar], i: int,
                  e5: list, e10: list, s, ctx: str, unit) -> None:
        """The 5/10 tranche of a Live symbol. Exits are as usual (opposite cross).
        Short entries, at most LIVE_MAX_ENTRIES_PER_DAY a session:
          * cross: a down-cross on any bar (9:35 and 9:40 included);
            stop = the day's high at entry;
          * state_0945: on the bar ending 9:45, only if no entry has been taken
            that day, if the 5 EMA is below the 10 EMA (no cross needed);
            stop = the opening-range high (9:30-9:45 bars).
        Stops are fixed at entry."""
        b, name = bars[i], sym["symbol"]
        tick = price_tick(b.close)
        c1 = ema_cross(e5, e10, i, tick)
        label = f"5/10 EMA crossed %s {ctx}"
        if c1 > 0:  # up: cover a short; long only in the model, never live
            if t is not None and t["side"] == "short":
                self._close(t, f.price, f.when, label % "up", s)
                t = None
            if t is None and sym["mode"] == "long_short":
                self._enter(sym, "EMA5_10", "long", f, unit(), s, label % "up")
            return
        if c1 < 0 and t is not None and t["side"] == "long":
            self._close(t, f.price, f.when, label % "down", s)
            t = None
        if t is not None:
            return
        end = b.end.astimezone(ET).time()
        day = [x for x in five if x.session == b.session and in_rth(x.start) and x.end <= b.end]
        today = [r for r in self.store.q(
            "SELECT entry_time FROM tranches WHERE symbol_id=? AND sleeve='EMA5_10' AND side='short' "
            "AND trigger IN (?, ?)", (sym["id"], *LIVE_TRIGGERS))
            if _dt(r["entry_time"]).astimezone(ET).date() == b.session]
        if c1 < 0:
            trig, stop = "cross", max(x.high for x in day)
            why = f"{label % 'down'}; stop = day's high at entry {stop:.4g}"
        elif end == LIVE_STATE_BAR_END and not today and ema_side(e5, e10, i, tick) < 0:
            trig, stop = "state_0945", max(x.high for x in day)
            why = (f"5/10 EMA state at 9:45: EMA5 {e5[i]:.4g} below EMA10 {e10[i]:.4g} on the "
                   f"{b.start.astimezone(ET):%b %d %H:%M}-{end:%H:%M} bar; stop = opening-range "
                   f"high {stop:.4g}; filled {f.how}")
        else:
            return
        if len(today) >= LIVE_MAX_ENTRIES_PER_DAY:
            self.store.log(f.when, "skip", f"{why}: live 5/10 - already "
                           f"{LIVE_MAX_ENTRIES_PER_DAY} entries today", name, "EMA5_10")
            return
        self._enter(sym, "EMA5_10", "short", f, unit(), s, why, trigger=trig, fixed_stop=stop)

    def _vwap_stop(self, t: dict, bars: list[Bar], i: int) -> float:
        """High-of-day stop, never widened: the session high at entry (or the
        entry price if higher), then lowered each new session to the lowest
        full-session high since entry - Russo's "trail it down as it rolls
        over". Rebuilt from the bars every time, so a restart can't lose it."""
        b, when = bars[i], _dt(t["entry_time"])
        start = when.astimezone(ET).date()
        at_entry = [x.high for x in bars[:i] if x.session == start and x.end <= when]
        if not at_entry:  # entry older than the loaded history
            return t["stop_price"]
        stop = max(max(at_entry), t["entry_price"])
        highs: dict = {}
        for x in bars[:i]:
            if start <= x.session < b.session:
                highs[x.session] = max(highs.get(x.session, x.high), x.high)
        return min([stop, *highs.values()])

    def _vwap_targets(self, t: dict, b: Bar, ma10, ma20, bar_txt: str, s) -> dict | None:
        """Cover into the daily 10-MA, or half there and the rest at the 20-MA
        (Russo's scale-out; Qullamaggie covers it all at the 10-MA). A bar that
        gaps below a target covers at its open. Returns the tranche if still open."""
        scale = s["vwap_scale_out_20"] and ma10 is not None and ma20 is not None and ma20 < ma10
        if not t["scaled"]:
            if ma10 is None or b.low > ma10:
                self.store.x("UPDATE tranches SET target_price=? WHERE id=?", (ma10, t["id"]))
                return t
            px = min(ma10, b.open)
            why = f"target: daily 10-MA {ma10:.4g} ({bar_txt} low touched it)"
            if not scale or t["qty"] < 2:
                self._close(t, px, b.end, why, s)
                return None
            t = self._partial_close(t, t["qty"] // 2, px, b.end, why + " - half covered", s)
        ma = ma20 if ma20 is not None else ma10
        if b.low <= ma:
            self._close(t, min(ma, b.open), b.end,
                        f"target 2: daily 20-MA {ma:.4g} ({bar_txt} low touched it)", s)
            return None
        self.store.x("UPDATE tranches SET target_price=? WHERE id=?", (ma, t["id"]))
        return t

    def _vwap_time_stop(self, t: dict, bars: list[Bar], i: int, fill: Fill, s) -> dict | None:
        """Cover at the last check of the Nth session held (entry day = 1), e.g.
        the 15:00 check on the hourly book. Returns the tranche if still open."""
        n = int(s["vwap_max_sessions"])
        if n <= 0:
            return t
        b, start = bars[i], _dt(t["entry_time"]).astimezone(ET).date()
        held = len({x.session for x in bars[:i + 1] if x.session >= start})
        last_check = session_bar_ends(b.session, self.minutes)[-2]
        if held > n or (held == n and b.end >= last_check):
            self._close(t, fill.price, fill.when, f"time stop: held {held} sessions "
                        f"(max {n}); covered {fill.how}", s)
            return None
        return t

    def _partial_close(self, t: dict, qty: int, price: float, when: datetime, reason: str,
                       s) -> dict:
        """Split `qty` shares off an open tranche and close them; the rest stays
        open (marked scaled). Risk and borrow already paid split pro rata."""
        frac = qty / t["qty"]
        fees = t["borrow_fees"] * frac
        part_id = self.store.x(
            """INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
               entry_price, stop_price, target_price, risk_dollars, fee_through, borrow_fees,
               scaled, trigger, grade, note, tags, stop_dist)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (t["symbol_id"], t["symbol"], t["sleeve"], t["side"], qty, t["entry_time"],
             t["entry_price"], t["stop_price"], t["target_price"], t["risk_dollars"] * frac,
             t["fee_through"], fees, t.get("trigger"), t.get("grade"), t.get("note"),
             t.get("tags"), t.get("stop_dist")))
        self._close({**t, "id": part_id, "qty": qty}, price, when, reason, s)
        rest = {**t, "qty": t["qty"] - qty, "risk_dollars": t["risk_dollars"] * (1 - frac),
                "borrow_fees": t["borrow_fees"] - fees, "scaled": 1}
        self.store.x("UPDATE tranches SET qty=?, risk_dollars=?, borrow_fees=?, scaled=1 "
                     "WHERE id=?", (rest["qty"], rest["risk_dollars"], rest["borrow_fees"], t["id"]))
        return rest

    def _ema_unit(self, bars: list[Bar], fast: list, slow: list, i: int, a: float, s) -> tuple:
        """Risk per share for a stop-less EMA trade, from this stock's own history:
        the 75th-percentile adverse close-to-close move of past cross-to-cross
        trades of the same EMA pair (as % of price, floored at 1 ATR). Returns
        ("pct", fraction, text) or ("abs", dollars, text)."""
        moves = cross_adverse_moves([b.close for b in bars], fast, slow, i)
        if len(moves) >= EMA_MIN_SAMPLES:
            p = percentile(moves, EMA_RISK_PCTL)
            floor = a / bars[i].close
            used = max(p, floor)
            return ("pct", used, f"risk/share = p75 adverse move of the last {len(moves)} "
                    f"cross-to-cross trades {p:.2%}" + (f" (floored at 1 ATR {floor:.2%})"
                                                          if floor > p else ""))
        d = s["stop_atr_mult"] * a
        return ("abs", d, f"risk/share = {s['stop_atr_mult']:g} x ATR {a:.4g} "
                          f"(only {len(moves)} past crosses)")

    def _ema_sleeve(self, sym, sleeve, t, f: Fill, a, s, down: bool, up: bool,
                    ctx: str = ""):
        label = ("5/10" if sleeve == "EMA5_10" else "10/20") + " EMA"
        label_ctx = f"{label} crossed %s {ctx}"
        if down:
            if t is not None and t["side"] == "long":
                self._close(t, f.price, f.when, (label_ctx % "down"), s)
                t = None
            if t is None:
                self._enter(sym, sleeve, "short", f, a, s, (label_ctx % "down"))
        elif up:
            if t is not None and t["side"] == "short":
                self._close(t, f.price, f.when, (label_ctx % "up"), s)
                t = None
            if t is None and sym["mode"] == "long_short":
                self._enter(sym, sleeve, "long", f, a, s, (label_ctx % "up"))

    # ------------------------------------------------------------ fills
    def _enter(self, sym, sleeve, side, f: Fill, unit: tuple | None, s, why: str,
               stop_level: float | None = None, target: float | None = None,
               trigger: str | None = None, fixed_stop: float | None = None) -> None:
        """stop_level: structural stop that also sizes the trade (VWAP).
        fixed_stop: a stop level for an EMA trade still sized by its EMA unit
        (live 5/10). stop_dist records entry-to-stop per share either way."""
        name = sym["symbol"]
        if sym["status"] != "active":
            self.store.log(f.when, "skip", f"{why}: symbol paused, no entry", name, sleeve)
            return
        late = self.day_only(sym, side, s)
        if sym.get("live") and f.when.astimezone(ET).time() == RTH_OPEN:
            self.store.log(f.when, "skip", f"{why}: IBKR live symbol - the signal came from the "
                           f"15:55-16:00 bar, after the 15:55 cutoff; not entered at the next open",
                           name, sleeve)
            return
        if late and f.when >= eod_cutoff(f.session):
            self.store.log(f.when, "skip", f"{why}: {late} - no new entries "
                           f"from {EOD_CUTOFF:%H:%M}", name, sleeve)
            return
        slip = s["slippage_bps"] / 1e4
        fill = f.price * (1 + slip) if side == "long" else f.price * (1 - slip)
        if stop_level is not None:  # structural stop (VWAP tranche: session HOD)
            stop = max(stop_level, fill) if side == "short" else min(stop_level, fill)
            dist = abs(stop - fill)
            math_txt = f"stop = high of day {stop:.4g}"
        else:
            kind, val, unit_txt = unit  # sizing only - EMA tranches have no stop
            dist = val * fill if kind == "pct" else val
            stop = fill - dist if side == "long" else fill + dist
            math_txt = f"no stop - exits only on the opposite cross; {unit_txt} = {dist:.4g}/share"
            if fixed_stop is not None:
                stop = fixed_stop
                math_txt = (f"fixed stop {stop:.4g} ({abs(stop - fill):.4g}/share); sized by "
                            f"{unit_txt} = {dist:.4g}/share")
        stop_dist = abs(stop - fill) if (stop_level is not None or fixed_stop is not None) else None
        if dist <= 0 or stop <= 0:
            self.store.log(f.when, "skip", f"{why}: invalid stop distance", name, sleeve)
            return
        eq = self.equity(s)
        budget = eq * sym["risk_pct"] / 100 / 3
        qty = math.floor(budget / dist)
        headroom = eq * s["max_leverage"] - self.gross_exposure()
        cap = math.floor(max(headroom, 0) / fill)
        capped = qty > cap
        qty = min(qty, cap)
        if qty < 1:
            self.store.log(f.when, "skip", f"{why}: size < 1 share "
                           f"(budget ${budget:,.0f}, buying-power headroom ${headroom:,.0f})",
                           name, sleeve)
            return
        self.store.x(
            """INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
               entry_price, stop_price, target_price, risk_dollars, fee_through, trigger, grade,
               stop_dist)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sym["id"], name, sleeve, side, qty, f.when.isoformat(), fill, stop, target,
             qty * dist, f.session.isoformat(), trigger, sym.get("grade"), stop_dist))
        note = " (capped by max leverage)" if capped else ""
        tgt = f", target {target:.2f}" if target is not None else ""
        self.store.log(f.when, "entry",
                       f"{why} -> {side.upper()} {qty} @ {fill:.2f}, "
                       f"{'stop ' + format(stop, '.4g') if stop_dist is not None else 'no stop'}{tgt}, "
                       f"risk ${qty * dist:,.0f}{note} [{math_txt}; size = "
                       f"${budget:,.0f} budget / {dist:.4g} per share]", name, sleeve)

    def _close(self, t: dict, price: float, when: datetime, reason: str, s) -> None:
        slip = s["slippage_bps"] / 1e4
        if t["side"] == "long":
            fill = price * (1 - slip)
            gross = (fill - t["entry_price"]) * t["qty"]
        else:
            fill = price * (1 + slip)
            gross = (t["entry_price"] - fill) * t["qty"]
        self.store.x(
            """UPDATE tranches SET exit_time=?, exit_price=?, exit_reason=?, gross_pnl=?,
               status='closed' WHERE id=?""",
            (when.isoformat(), fill, reason, gross, t["id"]))
        fees = self.store.q("SELECT borrow_fees FROM tranches WHERE id=?", (t["id"],))[0]["borrow_fees"]
        self.store.log(when, "exit",
                       f"{reason} -> closed {t['side']} {t['qty']} @ {fill:.2f}, "
                       f"P&L ${gross - fees:,.2f} net of ${fees:,.2f} borrow",
                       t["symbol"], t["sleeve"])

    def _accrue_borrow(self, t: dict, day: date, mark: float, s) -> None:
        nights = (day - date.fromisoformat(t["fee_through"])).days
        if nights <= 0:
            return
        fee = 0.0
        if t["side"] == "short":
            rate = self.borrow_pct(self._symbol(t["symbol_id"]), s)
            fee = nights * t["qty"] * mark * rate / 100 / s["borrow_day_count"]
        self.store.x("UPDATE tranches SET borrow_fees=borrow_fees+?, fee_through=? WHERE id=?",
                     (fee, day.isoformat(), t["id"]))
        t["borrow_fees"] += fee
        t["fee_through"] = day.isoformat()

    # ------------------------------------------------------------ reads
    def _symbol(self, symbol_id: int) -> dict:
        rows = self.store.q("SELECT * FROM symbols WHERE id=?", (symbol_id,))
        if not rows:
            raise ValidationError("unknown symbol")
        return rows[0]

    def _open(self, symbol_id: int | None = None) -> list[dict]:
        if symbol_id is None:
            return self.store.q("SELECT * FROM tranches WHERE status='open' ORDER BY id")
        return self.store.q("SELECT * FROM tranches WHERE status='open' AND symbol_id=? "
                            "ORDER BY id", (symbol_id,))

    def _marks(self) -> dict[int, float]:
        return {r["id"]: r["last_price"] for r in self.store.q("SELECT id, last_price FROM symbols")}

    def open_positions(self) -> list[dict]:
        marks = self._marks()
        out = []
        for t in self._open():
            m = marks.get(t["symbol_id"]) or t["entry_price"]
            sign = 1 if t["side"] == "long" else -1
            t["mark"] = m
            t["unrealized"] = sign * (m - t["entry_price"]) * t["qty"]
            t["notional"] = m * t["qty"]
            out.append(t)
        return out

    def gross_exposure(self) -> float:
        return sum(p["notional"] for p in self.open_positions())

    def equity(self, s: dict | None = None) -> float:
        s = s or self.store.settings()
        realized = self.store.q("SELECT COALESCE(SUM(gross_pnl),0) v FROM tranches "
                                "WHERE status='closed'")[0]["v"]
        fees = self.store.q("SELECT COALESCE(SUM(borrow_fees),0) v FROM tranches")[0]["v"]
        unreal = sum(p["unrealized"] for p in self.open_positions())
        return s["starting_capital"] + realized - fees + unreal

    def executions(self, start: date | None = None, end: date | None = None,
                   include_open: bool = False) -> list[dict]:
        """Trades as separate opening and closing executions (e.g. for a
        TradesViz import). A trade is included when it was opened between
        `start` and `end` (ET dates, inclusive); both of its legs come along,
        so no position is ever cut in half. Open trades only if asked: then
        just their opening leg. Prices are the model's fills (slippage
        included); borrow fees ride on the closing leg."""
        q = "SELECT * FROM tranches" + ("" if include_open else " WHERE status='closed'")
        out = []
        for t in self.store.q(q + " ORDER BY entry_time, id"):
            opened = _dt(t["entry_time"]).astimezone(ET)
            if (start and opened.date() < start) or (end and opened.date() > end):
                continue
            short = t["side"] == "short"
            legs = [(opened, "SELL" if short else "BUY", "Open", t["entry_price"], 0.0)]
            if t["status"] == "closed":
                legs.append((_dt(t["exit_time"]).astimezone(ET), "BUY" if short else "SELL",
                             "Close", t["exit_price"], t["borrow_fees"]))
            for when, action, kind, price, fees in legs:
                out.append({
                    "Date": when.strftime("%Y-%m-%d"), "Time": when.strftime("%H:%M:%S"),
                    "Symbol": t["symbol"], "Action": action,
                    "Direction": "Short" if short else "Long", "Type": kind,
                    "Quantity": t["qty"], "Price": round(price, 4), "Fees": round(fees, 2),
                    "Tranche": SLEEVE_LABELS[t["sleeve"]]
                    + (" - opening fade" if t.get("trigger") == "open_fade" else ""),
                    "TradeID": t["id"],
                })
        out.sort(key=lambda r: (r["Date"], r["Time"], r["TradeID"], r["Type"] != "Open"))
        return out

    def summary(self) -> dict:
        s = self.store.settings()
        closed = self.store.q("SELECT * FROM tranches WHERE status='closed' ORDER BY exit_time")
        opens = self.open_positions()
        eq = self.equity(s)
        for t in closed:
            t["net_pnl"] = t["gross_pnl"] - t["borrow_fees"]
            t["r_multiple"] = t["net_pnl"] / t["risk_dollars"] if t["risk_dollars"] else None

        def stats(rows):
            wins = [t["net_pnl"] for t in rows if t["net_pnl"] > 0]
            losses = [t["net_pnl"] for t in rows if t["net_pnl"] <= 0]
            rs = [t["r_multiple"] for t in rows if t["r_multiple"] is not None]
            return {
                "trades": len(rows),
                "win_rate": len(wins) / len(rows) if rows else None,
                "net_pnl": sum(wins) + sum(losses),
                "avg_win": sum(wins) / len(wins) if wins else None,
                "avg_loss": sum(losses) / len(losses) if losses else None,
                "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
                "avg_r": sum(rs) / len(rs) if rs else None,
                "borrow_fees": sum(t["borrow_fees"] for t in rows),
            }

        curve = self.store.q("SELECT ts, equity FROM equity ORDER BY ts")
        peak, max_dd = s["starting_capital"], 0.0
        for p in curve:
            peak = max(peak, p["equity"])
            max_dd = max(max_dd, (peak - p["equity"]) / peak if peak else 0)
        open_fees = sum(t["borrow_fees"] for t in opens)
        return {
            "equity": eq,
            "starting_capital": s["starting_capital"],
            "total_return": eq / s["starting_capital"] - 1,
            "realized_net": sum(t["net_pnl"] for t in closed),
            "unrealized": sum(p["unrealized"] for p in opens) - open_fees,
            "borrow_fees": sum(t["borrow_fees"] for t in closed) + open_fees,
            "gross_long": sum(p["notional"] for p in opens if p["side"] == "long"),
            "gross_short": sum(p["notional"] for p in opens if p["side"] == "short"),
            "max_drawdown": max_dd,
            "overall": stats(closed),
            "by_sleeve": {k: {**stats([t for t in closed if t["sleeve"] == k]),
                              "open": sum(1 for p in opens if p["sleeve"] == k),
                              "label": SLEEVE_LABELS[k]} for k in SLEEVES},
            "vwap_open_fade": {**stats([t for t in closed if t.get("trigger") == "open_fade"]),
                               "open": sum(1 for p in opens if p.get("trigger") == "open_fade"),
                               "label": "  of which: opening fade"},
            "by_side": {k: stats([t for t in closed if t["side"] == k]) for k in ("long", "short")},
            "by_symbol": {sym: stats([t for t in closed if t["symbol"] == sym])
                          for sym in sorted({t["symbol"] for t in closed})},
            "closed": closed[::-1][:300],
            "open": opens,
            "curve": curve,
        }
