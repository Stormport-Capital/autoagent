-- Recorded splits around the period (read-only). Used for the "no recorded
-- corporate action" suspect test and to find splits inside an event's bar window.
SELECT count(*) AS n,
       string_agg(concat_ws(',', trim(symbol), action_date, numerator, denominator,
                            coalesce(split_type, ''), source),
                  E'\n' ORDER BY action_date, symbol) AS csv
FROM core.corporate_actions
WHERE action_type = 'split' AND action_date BETWEEN '{start}' AND '{end}';
