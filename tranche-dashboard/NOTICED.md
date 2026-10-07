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
