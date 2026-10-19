# Noticed, not acted on

Found while narrowing the IBKR link to one book, the 5/10 short only, flat by
15:55 (the PR that added this file). Listed here on purpose: none of these were
changed. Each needs a decision before anyone works on it.

1. **No fallback price for live orders.** `BrokerSync._submit` (broker.py)
   looks up the model's `last_price` in the sync's own store. For the IBKR
   link, that store is `tranche_live.db`, which has no symbols, so no fallback
   price is ever found. If IBKR sends no bid, ask or last (for example, no
   market data subscription and `IBKR_MARKET_DATA_TYPE=1`), every IBKR order
   is refused with "no price to check the order against". This could block
   the first live one-share test.
2. **Only the stop cover is a market order.** The 15:55 flat, the reverse
   cross, Live off, the kill switch and the loss limit all cover with a
   marketable limit order (3% collar). An unfilled order is cancelled after
   45 s and re-priced about a minute later.
3. **Exits act at bar checks only.** The stop, cross and 15:55 exits are seen
   2 minutes after each 5-minute bar closes, later if the feed lags. Nothing
   rests at IBKR between checks.
4. **Live forces 15:55 flat on every tranche of that symbol in the 5-min
   model.** That includes the VWAP and 10/20 tranches, so they also stop
   holding overnight. This changes that symbol's model stats in the 5-min book.
5. **Model fill vs live fill.** The model fills at the bar close; the live
   order goes out after the check. `orders` rows are not linked to tranches,
   so per-trade slippage can only be matched by time (see the grading guide's
   Q2).
6. **`reset_portfolio` keeps `orders` and `broker_equity`** (store.py), so
   order history spans resets.
7. **Trade Review labels 15:55 and borrow-limit exits as "Other"**
   (`review.exit_type`).
8. **A `symbols.live` value set directly in another book's database** is
   ignored by the IBKR sync. It would still force 15:55 flat and the stop in
   that book's model. The UI and API refuse to set it outside the live book.
9. **Unknown: whether IBKR sends the shortable-shares tick (236) without a
   market data subscription.** Without it, IBKR's own locate check at order
   time decides.
10. **The loss-limit check between bars runs only while IBKR orders are
    switched on** (`Scheduler.run` → `LiveSync.watch`).

Added with the 9:45 state entry (same PR):

11. **Catch-up entries are late.** If the server is down at 9:47 and catches
    up later, the model still books the 9:45 entry at the 9:45 close. The
    IBKR sell goes out when the server is back, at that later price. This
    already applies to every entry, not only the new one.
12. **Trade Review shows neither `trigger` nor `stop_dist` for 5/10 trades.**
    Comparing `state_0945` with `cross`, or R measured from the stop, takes
    SQL for now.
13. **A cross entry's stop can sit only a hair above the fill** when the entry
    bar closes at its high. Any later close above it then stops the trade.
    That is the rule as written; this is a note, not a fix.

Added with the weekly cold restart:

14. **Switching a book to a new Alpaca account can stop its syncs.**
    `BrokerSync._refresh_orders` (broker.py) looks up every order still open
    in the book's `orders` table before each sync. The Alpaca adapter raises
    on an order the new account doesn't know, so every sync for that book
    fails until those rows are closed. The IBKR adapter treats an unknown
    order as expired (`ibkr.get_order`); the Alpaca one doesn't.


Added after the first live refusals (2026-10-16, XLAB):

15. **Only some refusals hold a short back for the day.** `_held_back`
    (broker.py) recognises a halt, "not tradable", "closing-only" / "no
    trading permission" and the not-shortable wordings. A short IBKR refuses
    for any other reason (its $2,000 minimum, a margin check) is re-sent at
    the next 5-minute check that day - once per check since 2026-10-19, no
    longer on every 5 s re-check within it. Holding back any opening short
    refused that day would stop it; not built.
16. **Unknown: why the borrow pre-check did not stop XLAB.** The tick-236
    check ran before each XLAB order and let it through; IBKR then refused
    every order with "The contract is not available for short sale". Either
    no shortable tick arrived within `quote_wait_s` (3 s), or IBKR reported
    shares its order-time check did not honour. The adapter does not record
    the borrow reading it acted on; logging it with each short would settle
    this.
17. **Only the live 5/10 tranche judges crosses against its recorded side.**
    The 2026-10-19 PMI miss (a cross that only showed once the feed had
    finished the bar) is fixed for `Engine._live_510` only. The paper books'
    10/20 tranche, the 5/10 tranche of non-live symbols and the VWAP-fail
    trigger still judge each bar once from the bars as served at that moment,
    so the same feed behaviour can still lose one of their signals. Extending
    the record to them changes the model's results; not done without a
    decision.
18. **The feed's bars are not recorded as judged.** The engine keeps only the
    EMA side it judged (`symbols.live_side`), not the close and EMAs the bar
    had at the check, and the snapshot is overwritten at the next check. So
    what the PMI 12:00 bar read at 12:02 cannot be shown, only inferred (the
    code is deterministic and the 12:07 replay of the same bars entered). A
    per-check record of the last few bars as read would settle such cases.
    Confirmed on the next bar the same day: the 12:00-12:05 bar read C14.30
    at 12:07 and C14.42 an hour later (Finviz Elite: 14.42). FMP's volume on
    the same bars also exceeds Finviz's (1,626 vs 1,120; 620 vs 364; 832 vs
    316), so FMP counts prints Finviz does not; 23 of 31 closes that morning
    differed by 1-14 cents, while the EMA math itself matched Finviz to the
    cent wherever the closes agreed.
19. **A symbol whose bar is late or absent keeps the retry loop busy.**
    `Engine.behind` counts a symbol as behind until its `last_bar_end`
    reaches the checked bar. When the feed has no such bar yet (a thin name
    with no trade in the window, or a slow print) the scheduler re-checks
    every 5 s for a minute and then every 3 min for the rest of the bar, one
    FMP request per symbol each time, and ran the broker link on every
    re-check. That turned one refused PMI order into 38 on 2026-10-19 (now
    one attempt per check). Treating a bar still missing after `STALE_WAIT`
    as not behind would end the loop sooner; not built.
