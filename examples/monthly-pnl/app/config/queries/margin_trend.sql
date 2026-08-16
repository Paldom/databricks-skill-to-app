-- @param start_date DATE
-- @param end_date DATE
SELECT
  DATE_FORMAT(`Booked Month`, 'yyyy-MM')  AS month,
  ROUND(MEASURE(`Gross Margin Pct`), 1)   AS gross_margin_pct
FROM demo.pnl_demo.pnl_metrics
WHERE `Booked Month` BETWEEN :start_date AND :end_date
GROUP BY ALL
ORDER BY month
