-- @param start_date DATE
-- @param end_date DATE
-- Grand total across the period. Every figure comes from the metric view, so the ratio
-- re-aggregates correctly instead of being averaged out of monthly percentages.
SELECT
  ROUND(MEASURE(`Revenue`), 0)          AS revenue,
  ROUND(MEASURE(`Gross Profit`), 0)     AS gross_profit,
  ROUND(MEASURE(`Gross Margin Pct`), 1) AS gross_margin_pct,
  ROUND(MEASURE(`Net Result`), 0)       AS net_result
FROM demo.pnl_demo.pnl_metrics
WHERE `Booked Month` BETWEEN :start_date AND :end_date
GROUP BY ALL
