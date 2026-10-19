-- Daily event candidates from core.ticker_history_daily_gold (read-only).
-- One row per (symbol, day) whose high reached :multiple x the close :lookback
-- rows earlier. CLOSE_* events are the subset whose close also got there.
-- Gold OHLC are split-adjusted, volume = raw / cum_split_factor, raw_close is
-- as-traded (trading-system scripts/migrations/048_create_ticker_history_daily_gold.sql).
-- Run once per calendar month (the full table is ~37M rows; a single pass
-- over the year times out). {month_start}, {month_end}, {lookback}, {multiple}
-- are filled in by events.py. Columns are emitted as one CSV text field.
WITH base AS (
  SELECT symbol, trade_date, source, open, high, low, close, raw_close, cum_split_factor, volume,
         lag(close, {lookback}) OVER w AS ref_close,
         lag(raw_close, {lookback}) OVER w AS ref_raw_close,
         lag(trade_date, {lookback}) OVER w AS ref_date,
         lag(cum_split_factor, {lookback}) OVER w AS ref_factor,
         avg(volume) OVER w20 AS avgvol20_adj,
         avg(volume::numeric * cum_split_factor) OVER w20 AS avgvol20_raw,
         count(*) OVER w20 AS n_prior
  FROM core.ticker_history_daily_gold
  WHERE trade_date BETWEEN DATE '{month_start}' - 45 AND DATE '{month_end}'
  WINDOW w AS (PARTITION BY symbol, source ORDER BY trade_date),
         w20 AS (PARTITION BY symbol, source ORDER BY trade_date
                 ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
)
SELECT count(*) AS n,
       string_agg(concat_ws(',', symbol, trade_date, source, open, high, low, close, raw_close,
                            cum_split_factor, volume, ref_close, ref_raw_close, ref_date, ref_factor,
                            round(avgvol20_adj, 1), round(avgvol20_raw, 1), n_prior),
                  E'\n' ORDER BY trade_date, symbol) AS csv
FROM base
WHERE trade_date BETWEEN DATE '{month_start}' AND DATE '{month_end}'
  AND ref_close > 0 AND high >= {multiple} * ref_close;
