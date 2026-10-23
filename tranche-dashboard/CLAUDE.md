# CLAUDE.md - tranche-dashboard

## Merging pull requests (Dean, 2026-10-23)

Merge your own tranche-dashboard pull requests once CI is green, there is no
merge conflict and no review thread is open. Don't wait for Dean. Give him
the PR link and say it's merged.

Run it by Dean first, and merge only after he says yes, when a change would
alter what trades or how much:

- entry or exit rules, stops, signals, or how bars and indicators are built
- share caps, order-size or loss limits, the kill switch
- which books or symbols can trade live, overnight or weekend holds
- paper vs live settings, or anything that sends orders to a new venue
- deleting or rewriting trading data

Speed, plumbing, logging, timing, dashboard display, tests and docs don't
need a sign-off, as long as the trading rules stay the same.

Merging never deploys. Dean deploys on the droplet himself (`IBKR.md`).
