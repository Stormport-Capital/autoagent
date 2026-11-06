# Read-only IBKR check. It never sends an order: the last step is a what-if,
# which only asks IBKR what a 1-share short would do to margin.
#
#   sudo -u tranche /opt/tranche/venv/bin/python -I ibkr_check.py SYMBOL [app dir]
#
# Run from the deployed tranche-dashboard directory (its .env is read). Prints
# OK/FAIL for the gateway connection, the $2,000 short minimum, market data,
# borrow (tick 236) and IBKR's own order checks for SELL 1 SYMBOL, then a one-line
# VERDICT naming the kind of block: closing-only (nobody may open it at IBKR), an
# account restriction (IBKR compliance flag), not shortable (IBKR lends none of it),
# no borrow now (IBKR lends it but has none at the moment), or tradeable.
import asyncio, dataclasses, math, sys, time
from pathlib import Path
APP = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import app, ibkr  # noqa: E402

SYM = (sys.argv[1] if len(sys.argv) > 1 else "F").upper()
INFO = {0, 300, 2104, 2106, 2107, 2108, 2158, 10167}
NO_DATA = {354, 10089, 10090, 10168}
UNSET = 1e300
app.load_dotenv(APP / ".env")
cfg = ibkr.config_from_env()
if cfg is None:
    sys.exit("FAIL IBKR_ACCOUNT is not set in .env")
cfg = dataclasses.replace(cfg, client_id=99, quote_wait_s=8.0)  # dashboard uses 17
b = ibkr.IBKRBroker(cfg)
errs = []
b.ib.errorEvent += lambda req, code, msg, *rest: errs.append((req, code, msg))
mark = lambda good: "OK  " if good else "FAIL"
usd = lambda x: "-" if x is None else f"${x:,.2f}"
num = lambda x: None if x is None or abs(float(x)) > UNSET or math.isnan(float(x)) else float(x)
try:
    a = None
    for _ in range(10):  # account values can take a moment after connecting
        try:
            a = b.account()
            break
        except ibkr.BrokerError as e:
            if "not loaded yet" not in str(e):
                sys.exit(f"FAIL {e}")
            time.sleep(1)
    if a is None:
        sys.exit("FAIL IBKR sent no account values")
    print(f"OK   connected to {b.endpoint} (check client 99)")
    print(f"{mark(a['equity'] >= 2000)} net liquidation {usd(a['equity'])} (shorting needs $2,000 or more)")
    print(f"     cash {usd(a['cash'])}, buying power {usd(a['buying_power'])}")
    pos = b.positions()
    held = ", ".join(f"{s} {p['qty']}" for s, p in pos.items()) or "none"
    print(f"     positions: {held}")

    q = b.quote(SYM)
    got = q["bid"] or q["ask"] or q["last"]
    nodata = [m for _, c, m in errs if c in NO_DATA]
    print(f"{mark(got and not nodata)} market data for {SYM}: bid {q['bid']}, ask {q['ask']}, "
          f"last {q['last']}, close {q['close']}")
    for m in nodata:
        print(f"     IBKR: {m}")
    lvl, sh = q["shortable"], q["shortable_shares"]
    kind = ("no data" if lvl is None else "easy to borrow" if lvl > 2.5
            else "locate needed" if lvl > 1.5 else "NOT shortable now")
    print(f"{mark(lvl is not None and lvl > 2.5)} borrow for {SYM}: {kind}"
          + ("" if sh is None else f", {sh:,.0f} shares available"))

    c = b._contract(SYM)
    px = q["bid"] or q["last"] or q["close"]
    order = (b.api.LimitOrder("SELL", 1, ibkr.limit_price("sell", px, cfg.collar_pct)) if px
             else b.api.MarketOrder("SELL", 1))
    order.account, order.tif, order.outsideRth = cfg.account, "DAY", False
    n0 = len(errs)
    try:  # whatIfOrderAsync copies the order and sets whatIf=True: never transmitted
        st = b._run(lambda: asyncio.wait_for(b.ib.whatIfOrderAsync(c, order), 20), timeout=30)
    except Exception as e:
        st = None
        print(f"     what-if: no answer ({type(e).__name__})")
    if not hasattr(st, "initMarginChange"):  # a refusal (error 201) answers with no OrderState
        st = None
    time.sleep(2)  # IBKR can send its refusal just after the answer
    refusals = [(code, m) for _, code, m in errs[n0:] if code not in INFO]
    good = st is not None and num(st.initMarginChange) is not None and not refusals
    print(f"{mark(good)} what-if: SELL 1 {SYM} (short) - "
          + ("accepted by IBKR's checks" if good else "refused or unanswered"))
    if st is not None:
        print(f"     initial margin +{usd(num(st.initMarginChange))}, maintenance "
              f"+{usd(num(st.maintMarginChange))}, commission "
              f"{usd(num(st.commission)) if num(st.commission) is not None else usd(num(st.minCommission))}"
              + (f", warning: {st.warningText}" if st.warningText else ""))
    for code, m in refusals:
        print(f"     IBKR {code}: {m}")
    other = [(code, m) for _, code, m in errs[:n0] if code not in INFO | NO_DATA]
    for code, m in other:
        print(f"     note {code}: {m}")
    # One line on which kind of block this is, so a broker question can be told from a
    # timing one. IBKR's refusal (if any) wins over the borrow reading: a stock nobody
    # may open, or this account may not, is blocked whatever the borrow says.
    msgs = " ".join(m for _c, m in refusals)
    if "closing-only" in msgs and "regulatory" in msgs.lower():
        why = ("CLOSING-ONLY (regulatory): nobody may open a position in this stock, and a "
               "regulatory restriction is likely the same at every broker. Only waiting it out helps")
    elif "closing-only" in msgs:
        why = ("CLOSING-ONLY (IBKR risk management): IBKR lets nobody open a position in this "
               "stock today, whatever the borrow. Waiting it out or another broker helps")
    elif "No Trading Permission" in msgs or "Compliance Restriction" in msgs:
        why = ("ACCOUNT RESTRICTION: this account may not open positions in this stock (IBKR "
               "compliance flag). An IBKR ticket or call, not a borrow problem")
    elif refusals:
        why = f"REFUSED by IBKR: {refusals[0][1][:120]}"
    elif lvl is None:
        why = "UNKNOWN: IBKR sent no borrow data; an order would face IBKR's own locate check"
    elif lvl <= 1.5:
        why = ("NOT SHORTABLE: IBKR has no lending in this name at all. A locate broker might, "
               "at a per-share fee; no IBKR setting changes it")
    elif lvl <= 2.5:
        why = ("NO BORROW NOW: IBKR lends this name but has none at the moment. Timing and "
               "broker: recheck after 9:30, or a locate broker could fill it")
    elif good:
        why = "TRADEABLE: easy to borrow and IBKR accepts the short"
    else:
        why = "UNANSWERED: IBKR did not answer the what-if; run the check again"
    print(f"VERDICT {SYM}: {why}")
    print("Nothing was ordered. Done.")
finally:
    try:
        b._run(b.ib.disconnect)
    except Exception:
        pass
