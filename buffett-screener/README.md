# Buffett weekday quality screener

A weekday-scheduled screen for Buffett-style quality compounders. It runs on
**GitHub Actions** (whose runners have internet), pulls fundamentals from the
Financial Modeling Prep (FMP) API, and commits a dated candidate report to
`reports/`.

## Why it runs on GitHub Actions and not in a Claude session

The Claude Code sandbox's network policy blocks all external market-data hosts
(FMP, Yahoo, Polygon, etc. all return `403` at the egress proxy). A scheduled
Claude session inherits that policy, so it cannot fetch fundamentals. GitHub's
hosted runners are outside that policy and can, so the scan lives here. (If your
admin later allowlists a data host in the environment's network policy, the same
`screener.py` can be driven from a Claude Routine instead — the code is the same.)

## What it is — and is NOT

- It automates only the **~62% quantitative proxy layer**. Every threshold is a
  **secondary-codifier proxy** (Mary Buffett & David Clark; David Braverman via
  Brendan Boyd) — **not** Buffett's own rule.
- Buffett's real engine — **moat durability** and **intrinsic value** — is a
  C4 judgment he says he never formulates. It is deliberately **excluded** and
  routed to [`research_checklist.md`](research_checklist.md).
- **A passing name is a research candidate, never a buy.**
- The **ROE gate is unresolved** between the two codifiers (≥12% vs ≥20%). The
  screen reports **both** tiers and never picks for you.

## Setup (one time)

1. **Add the FMP key as a secret** (never commit it):
   `Settings → Secrets and variables → Actions → New repository secret`
   - Name: `FMP_API_KEY`  ·  Value: your FMP key.
   - The key you pasted in chat is exposed — **rotate it** and store the new one here.
2. **Merge this to the default branch.** Scheduled workflows only fire from the
   default branch; on a feature branch the workflow is inert (safe to review first).
3. Optionally test immediately: `Actions → Buffett weekday quality scan → Run workflow`.

## Schedule

`.github/workflows/buffett-weekday-scan.yml` runs `cron: 0 12 * * 1-5` =
**08:00 America/New_York during EDT**, Mon–Fri. Cron is UTC and does not follow
DST, so from November (EST) it lands at 07:00 ET until you bump the hour.

## Output & delivery

- A dated file `reports/buffett_scan_YYYY-MM-DD.md` plus `reports/latest.md`,
  committed to the repo each run, and uploaded as a downloadable **artifact**.
- **Email** is built in and **self-skips** until configured. To turn it on, add
  three repo secrets: `MAIL_TO` (recipient), `MAIL_USERNAME` (Gmail address),
  `MAIL_PASSWORD` (a Gmail **app password** — https://myaccount.google.com/apppasswords,
  needs 2FA). With them set, the report is emailed every weekday run; without
  them the step is skipped and the job still succeeds.
- **Drive doc / Gmail draft** (your original ask) aren't native to Actions —
  they need Google credentials Actions doesn't have. The clean way to get them
  is a small **Claude Routine** that reads `reports/latest.md` from the repo
  after each run and mirrors it to your Drive Buffett folder + a Gmail draft.
  Ask and I'll wire that half.

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `FMP_API_KEY` | — (required) | FMP API key, from the Actions secret. |
| `MAX_TICKERS` | `100` | Cap on names screened; free tier is ~250 calls/day, 2 calls/ticker. |
| `DEEP` | off | `1` = exact long-term-debt/net-earnings test (4 calls/ticker) instead of the debt/equity proxy. |
| `UNIVERSE_FILE` | `universe.txt` | One ticker per line; `#` comments. |

## Signals

Defined in [`signals.yaml`](signals.yaml) with per-signal provenance. Core
gates (all must pass): net margin > 15%, ROIC ≥ 12%, conservative leverage,
positive EPS, plus the dual ROE tiers. Earnings-yield-vs-Treasury and P/E are
contextual (reported, not used to reject).

## Important limitations (read before trusting a run)

- **Not point-in-time.** FMP TTM endpoints reflect latest/restated data, so this
  is a *current-state* screen, not a backtest engine. The starter `universe.txt`
  is survivorship-biased (known survivors) — **do not** backtest on it. A real
  historical test needs a point-in-time, restatement-free, delisted-inclusive
  source (see the data-requirements manifest in the DoctrineKB bundle).
- **Thresholds are proxies, not Buffett's words** — a positive result validates
  Mary Buffett/Braverman's screen, not "Buffett's method."
- **Date-bound numbers** (e.g. the $20M→~$40M owner-earnings floor, the "6%
  Treasury" baseline) are rebased or fetched live where possible; the report
  labels the Treasury value as FALLBACK when the live fetch fails.

## Run locally

```bash
cd buffett-screener
pip install -r requirements.txt
export FMP_API_KEY=...        # requires a network path to FMP (not the sandbox)
python screener.py            # writes reports/buffett_scan_<date>.md
```

## Tests

The decision logic is factored into a pure `score()` function (plus
`leverage_test()`), so it can be validated with no network or key:

```bash
cd buffett-screener
python test_screener.py       # 28 offline checks: scoring, boundaries, config, universe
```

`.github/workflows/buffett-screener-ci.yml` runs this on every push/PR that
touches the screener — a green check means the signal logic, thresholds, and
universe file are all consistent.
