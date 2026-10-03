# Tranche-dashboard event backtest

A strategy comparison, not a model fit: the four autoagent tranche-dashboard
entries (5/10 EMA cross, 10/20 EMA cross, VWAP fail "Russo Trigger B", opening
fade) on 1h, 15m and 5m bars, run on stocks that went up 100%+ in one day. The
rules are never copied: the backtest calls `tranche-dashboard/engine.py` and
only feeds it bars and reads its trades back.

## Status (2026-10-02)

The cloud session ended after Step 2 and the code for Steps 3-5. The rest
runs on Dean's PC: **read [HANDOFF.md](HANDOFF.md) first** — every decision,
the open questions, what is built and tested, and the exact commands.

## Files

See the table in [HANDOFF.md](HANDOFF.md#whats-built-and-tested).

## Re-running

Commands are in [HANDOFF.md](HANDOFF.md#exact-commands-run-inside-the-worktree).
`EVENT_PRICE_BASIS` / `EVENT_VOLUME_BASIS` = `as_traded` (default, Dean's
decision) or `adjusted` (reproduces the earlier counts). Other event
definitions (up 100% in 2 days, up 300% in 5 days) use sql/candidates.sql with
a different `{lookback}` / `{multiple}` and the matching entries in spec.py.
