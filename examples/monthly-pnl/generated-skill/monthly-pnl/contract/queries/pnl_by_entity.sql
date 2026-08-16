-- @param start_date DATE
-- @param end_date DATE
-- @param row_limit INT
-- ORDER BY names the SELECT alias, not MEASURE(`Net Result`): Spark rejects the latter with
-- METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT. The trailing `entity` is the tie-breaker that
-- makes LIMIT a total order rather than an arbitrary sample.
SELECT
  `Entity`                    AS entity,
  ROUND(MEASURE(`Revenue`), 0)          AS revenue,
  ROUND(MEASURE(`Gross Margin Pct`), 1) AS gross_margin_pct,
  ROUND(MEASURE(`Net Result`), 0)       AS net_result
FROM demo.pnl_demo.pnl_metrics
WHERE `Booked Month` BETWEEN :start_date AND :end_date
GROUP BY ALL
ORDER BY net_result DESC, entity
LIMIT :row_limit
