# Read-only TradeZero check. It never sends an order. The one optional write is
# a locate QUOTE (--locate-quote, live accounts): that asks TradeZero the price
# of borrowing 100 shares and prints it; nothing is accepted or bought.
#
#   sudo -u tranche /opt/tranche/venv/bin/python -I tz_check.py SYMBOL [--locate-quote] [app dir]
#
# Run from the deployed tranche-dashboard directory (its .env is read). Prints
# OK/FAIL for the API keys and account, the account type against TZ_MODE, the
# account status, equity and buying power, positions, the live route, whether
# SYMBOL is easy to borrow, any located shares, and a one-line VERDICT.
import os, sys, time
from datetime import datetime
from pathlib import Path
args = [a for a in sys.argv[1:] if not a.startswith("--")]
flags = {a for a in sys.argv[1:] if a.startswith("--")}
APP = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import app, tradezero  # noqa: E402
from broker import BrokerError  # noqa: E402
from data import make_provider  # noqa: E402
from indicators import ET  # noqa: E402


def share_price(sym):
    """(last 5-minute close, source) from the dashboard's own data provider - the
    reference the locate fee cap is a percent of. (None, why) when it has none."""
    name = ((os.environ.get("TRANCHE_PROVIDER") or "").strip().lower()
            or ("polygon" if os.environ.get("POLYGON_API_KEY") else
                "fmp" if os.environ.get("FMP_API_KEY") else "yahoo"))
    try:
        bars = make_provider(name).five_min_bars(sym, datetime.now(ET))
    except Exception as e:  # the check goes on without a price
        return None, f"{name}: {type(e).__name__}"
    return (bars[-1].close, name) if bars else (None, f"{name}: no bars")

SYM = (args[0] if args else "F").upper()
mark = lambda good: "OK  " if good else "FAIL"
usd = lambda x: "-" if x is None else f"${x:,.2f}"
app.load_dotenv(APP / ".env")
try:
    cfg = tradezero.config_from_env()
except BrokerError as e:
    sys.exit(f"FAIL {e}")
if cfg is None:
    sys.exit("FAIL TZ_ACCOUNT is not set in .env (see TRADEZERO.md)")
b = tradezero.TradeZeroBroker(cfg)
try:
    row = b._account_row()  # raises on a key / account / mode problem
except BrokerError as e:
    sys.exit(f"FAIL {e}")
print(f"OK   keys see account {cfg.account}: type {row.get('accountType')}, matches TZ_MODE={cfg.mode}")
status = str(row.get("accountStatus") or "?")
print(f"{mark(status == 'Active')} account status {status}" + ("" if status == "Active" else
      " (TradeZero only takes orders from an Active account)"))
a = b.account()
print(f"     equity {usd(a['equity'])}, cash {usd(a['cash'])}, buying power {usd(a['buying_power'])}"
      + ("" if b.day_pnl() is None else f", today's P&L {b.day_pnl():+,.2f}"))
print(f"{mark(a['equity'] >= 2000)} equity {usd(a['equity'])} (shorting at a US broker needs $2,000 or more)")
pos = b.positions()
print("     positions: " + (", ".join(f"{s} {p['qty']}" for s, p in pos.items()) or "none"))
print(f"     size: " + (f"{cfg.shares} shares per symbol, fixed (TZ_SHARES)" if cfg.shares else
                        f"the model's size, at most {cfg.max_shares} share(s) (TZ_MAX_SHARES)")
      + f"; order value cap {usd(cfg.max_order_usd)}; daily loss limit {usd(cfg.daily_loss_limit)}")
if b.live:
    try:
        print(f"OK   live route: {b._route_name()}")
    except BrokerError as e:
        print(f"FAIL {e}")
else:
    print("     paper account: routes itself (PAPER)")
etb = b.easy_to_borrow(SYM)
print(f"{mark(etb)} {SYM} easy to borrow: {etb}" + (" (paper lists every symbol as easy to borrow; "
      "the live list is the one that counts)" if not b.live else ""))
inv = 0
if b.live:
    inv = b.inventory(SYM)
    print(f"     located shares of {SYM} on the account: {inv}")
    spent = b.locate_spend_today()
    print(f"     locates bought today: {usd(spent)} (cap TZ_LOCATE_MAX_USD_DAY "
          f"{usd(cfg.locate_max_usd_day)}; fee cap TZ_LOCATE_MAX_PCT {cfg.locate_max_pct:.2f}% "
          f"of the share price" + (", locates OFF" if cfg.locate_max_pct <= 0 else "") + ")")
px, src = share_price(SYM)
print(f"     {SYM} share price: " + (f"${px:,.2f} (last 5-minute close, {src})" if px
                                     else f"none ({src}) - the bot refuses a locate without one"))
offer = None
if "--locate-quote" in flags and b.live and etb is False and inv == 0:
    qid = f"tzcheck-{SYM}-{int(time.time())}"
    b._req("loc_quote", "POST", "/accounts/locates/quote",
           json={"account": cfg.account, "symbol": SYM, "quantity": 100, "quoteReqID": qid})
    rows = []
    for _ in range(5):
        time.sleep(2)
        rows = [r for r in b.locate_history() if str(r.get("quoteReqID") or "") in (qid, qid + ".SU")]
        if any(r.get("locateStatus") == 65 for r in rows):
            break
    offers = [r for r in rows if r.get("locateStatus") == 65 and float(r.get("locatePrice") or 0) > 0]
    for r in rows:
        print(f"     locate quote {r.get('quoteReqID')}: status {r.get('locateStatus')}, "
              f"${float(r.get('locatePrice') or 0):.4f}/share, type {r.get('locateType')}"
              + (f", {r.get('text')}" if r.get("text") else ""))
    if offers:
        offer = min(float(r["locatePrice"]) for r in offers)
        if px:
            print(f"     = {offer / px * 100:.2f}% of the ${px:,.2f} share price, ${offer * 100:.2f} per 100")
    print("     (quote only - nothing accepted, nothing bought; the offer lapses in 30 s)")
elif "--locate-quote" in flags and not b.live:
    print("     --locate-quote ignored: paper accounts have no locates")

if status != "Active":
    why = f"ACCOUNT NOT ACTIVE ({status}): TradeZero takes no orders; a TradeZero support question"
elif etb:
    why = "TRADEABLE: easy to borrow at TradeZero" + ("" if b.live else " (on paper, everything is)")
elif inv > 0:
    why = f"TRADEABLE: {inv} located shares already on the account"
elif not b.live:
    why = "NOT EASY TO BORROW on paper: paper can't locate, so the dashboard skips the short"
elif cfg.locate_max_pct <= 0:
    why = ("NOT EASY TO BORROW: needs a locate and locates are off (TZ_LOCATE_MAX_PCT=0); "
           "the dashboard skips the short")
elif offer is not None and not px:
    why = (f"LOCATE AVAILABLE at ${offer:.4f}/share (${offer * 100:.2f} per 100), but no share "
           f"price to judge it against; the bot would not buy it until the model has one")
elif offer is not None:
    pct = offer / px * 100
    why = (f"LOCATE AVAILABLE at ${offer:.4f}/share = {pct:.2f}% of the ${px:,.2f} share price "
           f"(${offer * 100:.2f} per 100): " + ("within" if pct <= cfg.locate_max_pct else "ABOVE")
           + f" the cap TZ_LOCATE_MAX_PCT {cfg.locate_max_pct:.2f}%")
elif "--locate-quote" in flags:
    why = "NO BORROW NOW: TradeZero offered no shares to locate; try again later"
else:
    why = ("NOT EASY TO BORROW: a locate would be needed; run again with --locate-quote to see "
           "the price (nothing is bought)")
print(f"VERDICT {SYM}: {why}")
print("Nothing was ordered. Done.")
