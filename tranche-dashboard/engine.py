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

EMA tranches have a hard stop at stop_atr_mult x hourly ATR from entry.
Sizing: qty = (equity x risk_pct / 3) / stop distance, capped so gross exposure
stays <= equity x max_leverage. Fills are simulated at the hourly close (entries,
signal exits) or at the stop / gapped open (stops), with slippage_bps adverse.
Short tranches pay borrow_rate_pct / borrow_day_count per calendar night held.
Only hourly bars that complete after the symbol was added are ever traded.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta

from indicators import (ET, Bar, atr, crossed_above, crossed_below, daily_closes,
                        daily_sma, ema, resample_hourly, session_vwap)
from data import redact
from store import DEFAULT_SETTINGS, Store

SLEEVES = ("EMA5_10", "EMA10_20", "VWAP")
SLEEVE_LABELS = {
    "EMA5_10": "5/10 EMA cross",
    "EMA10_20": "10/20 EMA cross",
    "VWAP": "VWAP fail (Russo)",
}
RISK_MIN, RISK_MAX = 0.5, 3.0
STALE_WAIT = timedelta(minutes=20)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

SETTING_BOUNDS = {
    "starting_capital": (1_000.0, 1e9),
    "borrow_rate_pct": (0.0, 200.0),
    "borrow_day_count": (360, 365),
    "stop_atr_mult": (0.25, 10.0),
    "atr_period": (2, 50),
    "max_leverage": (0.1, 10.0),
    "slippage_bps": (0.0, 200.0),
    "bar_close_delay_min": (0, 30),
}


class ValidationError(ValueError):
    pass


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


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
    def __init__(self, store: Store, provider):
        self.store = store
        self.provider = provider

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

    def add_symbols(self, raw: str, mode: str, risk_pct, now: datetime) -> list[str]:
        if mode not in ("short_only", "long_short"):
            raise ValidationError("mode must be short_only or long_short")
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
                "INSERT INTO symbols (symbol, mode, risk_pct, added_at) VALUES (?,?,?,?)",
                (sym, mode, risk, now.isoformat()))
            self.store.log(now, "added", f"watching {sym}: {mode.replace('_', ' ')}, "
                           f"risk {risk:g}% of equity", sym)
            added.append(sym)
        if not added:
            raise ValidationError("no new symbols to add")
        return added

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
    def tick(self, now: datetime) -> None:
        with self.store.lock:
            s = self.store.settings()
            for sym in self.store.q("SELECT * FROM symbols WHERE status!='removed' ORDER BY id"):
                try:
                    self._process_symbol(sym, now, s)
                except Exception as e:  # one bad symbol must not stop the rest
                    msg = redact(e)[:300]
                    self.store.x("UPDATE symbols SET error=? WHERE id=?", (msg, sym["id"]))
                    self.store.log(now, "error", msg, sym["symbol"])
            self.store.record_equity(now, self.equity(s))

    def behind(self, bar_end: datetime) -> bool:
        """True if any watched symbol has not yet processed the hourly bar
        ending at `bar_end` (e.g. a delayed feed had not printed it yet)."""
        for r in self.store.q("SELECT added_at, last_bar_end FROM symbols WHERE status!='removed'"):
            if _dt(r["added_at"]) >= bar_end:
                continue
            if r["last_bar_end"] is None or _dt(r["last_bar_end"]) < bar_end:
                return True
        return False

    def _process_symbol(self, sym: dict, now: datetime, s: dict) -> None:
        five = self.provider.five_min_bars(sym["symbol"], now)
        delay = timedelta(minutes=s["bar_close_delay_min"])
        # an hour counts as complete once the feed has printed through its end
        # (delayed feeds lag), or 20 min later for names with no late prints
        data_end = max((b.end for b in five), default=None)
        bars = [b for b in resample_hourly(five)
                if b.end + delay <= now
                and ((data_end and data_end >= b.end) or b.end + max(delay, STALE_WAIT) <= now)]
        if not bars:
            raise RuntimeError("no completed hourly bars returned")
        closes = [b.close for b in bars]
        ind = {
            "e5": ema(closes, 5), "e10": ema(closes, 10), "e20": ema(closes, 20),
            "atr": atr(bars, int(s["atr_period"])),
            "daily": daily_closes(five),
        }
        added = _dt(sym["added_at"])
        last = _dt(sym["last_bar_end"])
        for i, b in enumerate(bars):
            if b.end <= added or (last and b.end <= last):
                continue
            self._process_bar(sym, bars, five, i, ind, s)
            last = b.end
            self.store.x("UPDATE symbols SET last_bar_end=?, last_price=? WHERE id=?",
                         (last.isoformat(), b.close, sym["id"]))
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

    def _process_bar(self, sym, bars: list[Bar], five: list[Bar], i: int, ind: dict, s: dict):
        b = bars[i]
        prev = bars[i - 1] if i > 0 else None
        name = sym["symbol"]
        open_tr = {t["sleeve"]: t for t in self._open(sym["id"])}

        # 1. borrow fees for shorts carried overnight, marked at the prior close
        if prev is not None:
            for t in open_tr.values():
                self._accrue_borrow(t, b.session, prev.close, s)

        # 2. exits against this bar's range, stop before target
        hod = max(x.high for x in bars[:i + 1] if x.session == b.session)  # incl. this bar
        target = daily_sma(ind["daily"], b.session, 10)
        for sleeve, t in list(open_tr.items()):
            if b.end <= _dt(t["entry_time"]):
                continue
            if sleeve == "VWAP":  # Russo: new high of day stop, daily 10-MA target
                stop = max(hod, t["entry_price"])
                if b.high >= stop:
                    self._close(t, stop, b.end, f"stop: new high of day {stop:.2f}", s)
                elif target is not None and b.low <= target:
                    # a gap below the target covers at the open, not above the bar
                    self._close(t, min(target, b.open), b.end,
                                f"target: daily 10-MA {target:.2f}", s)
                else:
                    self.store.x("UPDATE tranches SET stop_price=?, target_price=? WHERE id=?",
                                 (stop, target, t["id"]))
                    continue
                del open_tr[sleeve]
                continue
            stop = t["stop_price"]  # EMA tranches: fixed ATR stop, gap = fill at open
            if t["side"] == "short" and b.high >= stop:
                self._close(t, max(b.open, stop), b.end, "stop hit", s)
            elif t["side"] == "long" and b.low <= stop:
                self._close(t, min(b.open, stop), b.end, "stop hit", s)
            else:
                continue
            del open_tr[sleeve]

        # 3. signals at the hourly close
        e5, e10, e20, a = ind["e5"], ind["e10"], ind["e20"], ind["atr"][i]
        j = i - 1
        ema_ready = i > 0 and e20[j] is not None and a is not None
        if not ema_ready:
            self.store.log(b.end, "warmup", "not enough hourly history for EMA20/ATR yet", name)
        else:
            self._ema_sleeve(sym, "EMA5_10", open_tr.get("EMA5_10"), b, a, s,
                             crossed_below(e5[j], e10[j], e5[i], e10[i]),
                             crossed_above(e5[j], e10[j], e5[i], e10[i]))
            self._ema_sleeve(sym, "EMA10_20", open_tr.get("EMA10_20"), b, a, s,
                             crossed_below(e10[j], e20[j], e10[i], e20[i]),
                             crossed_above(e10[j], e20[j], e10[i], e20[i]))

        vwap = session_vwap(five, b.session, b.end)
        if vwap is None:
            return
        # Russo Trigger B, rising edge only: fires on the bar where it turns true
        lost = vwap_fail(bars, five, i) and not (i > 0 and vwap_fail(bars, five, i - 1))
        if lost and "VWAP" not in open_tr:
            self._enter(sym, "VWAP", "short", b, None, s,
                        f"VWAP fail: close {b.close:.2f} < VWAP {vwap:.2f}, "
                        f"high {b.high:.2f} below HOD {hod:.2f}",
                        stop_level=hod, target=target)

    def _ema_sleeve(self, sym, sleeve, t, b: Bar, a: float, s, down: bool, up: bool):
        label = "5/10" if sleeve == "EMA5_10" else "10/20"
        if down:
            if t is not None and t["side"] == "long":
                self._close(t, b.close, b.end, f"{label} EMA crossed down", s)
                t = None
            if t is None:
                self._enter(sym, sleeve, "short", b, a, s, f"{label} EMA crossed down")
        elif up:
            if t is not None and t["side"] == "short":
                self._close(t, b.close, b.end, f"{label} EMA crossed up", s)
                t = None
            if t is None and sym["mode"] == "long_short":
                self._enter(sym, sleeve, "long", b, a, s, f"{label} EMA crossed up")

    # ------------------------------------------------------------ fills
    def _enter(self, sym, sleeve, side, b: Bar, a: float | None, s, why: str,
               stop_level: float | None = None, target: float | None = None) -> None:
        name = sym["symbol"]
        if sym["status"] != "active":
            self.store.log(b.end, "skip", f"{why}: symbol paused, no entry", name, sleeve)
            return
        slip = s["slippage_bps"] / 1e4
        fill = b.close * (1 + slip) if side == "long" else b.close * (1 - slip)
        if stop_level is not None:  # structural stop (VWAP tranche: session HOD)
            stop = max(stop_level, fill) if side == "short" else min(stop_level, fill)
            dist = abs(stop - fill)
        else:
            dist = s["stop_atr_mult"] * a
            stop = fill - dist if side == "long" else fill + dist
        if dist <= 0 or stop <= 0:
            self.store.log(b.end, "skip", f"{why}: invalid stop distance", name, sleeve)
            return
        eq = self.equity(s)
        budget = eq * sym["risk_pct"] / 100 / 3
        qty = math.floor(budget / dist)
        headroom = eq * s["max_leverage"] - self.gross_exposure()
        cap = math.floor(max(headroom, 0) / fill)
        capped = qty > cap
        qty = min(qty, cap)
        if qty < 1:
            self.store.log(b.end, "skip", f"{why}: size < 1 share "
                           f"(budget ${budget:,.0f}, buying-power headroom ${headroom:,.0f})",
                           name, sleeve)
            return
        self.store.x(
            """INSERT INTO tranches (symbol_id, symbol, sleeve, side, qty, entry_time,
               entry_price, stop_price, target_price, risk_dollars, fee_through)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sym["id"], name, sleeve, side, qty, b.end.isoformat(), fill, stop, target,
             qty * dist, b.session.isoformat()))
        note = " (capped by max leverage)" if capped else ""
        tgt = f", target {target:.2f}" if target is not None else ""
        self.store.log(b.end, "entry",
                       f"{why} -> {side.upper()} {qty} @ {fill:.2f}, stop {stop:.2f}{tgt}, "
                       f"risk ${qty * dist:,.0f}{note}", name, sleeve)

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
            fee = nights * t["qty"] * mark * s["borrow_rate_pct"] / 100 / s["borrow_day_count"]
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
            "by_side": {k: stats([t for t in closed if t["side"] == k]) for k in ("long", "short")},
            "by_symbol": {sym: stats([t for t in closed if t["symbol"] == sym])
                          for sym in sorted({t["symbol"] for t in closed})},
            "closed": closed[::-1][:300],
            "open": opens,
            "curve": curve,
        }
