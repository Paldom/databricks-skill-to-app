-- Synthetic P&L fact table for the worked example.
-- Deterministic on purpose: pseudo-random amounts are derived from the row index, so re-running
-- this file reproduces the same numbers and the example stays comparable across workspaces.
CREATE OR REPLACE TABLE demo.pnl_demo.pnl_fact
COMMENT 'Synthetic monthly P&L postings. Demo data - not real financials.'
AS
WITH months AS (
  SELECT explode(sequence(DATE'2025-09-01', DATE'2026-08-01', INTERVAL 1 MONTH)) AS booked_on
),
entities AS (
  -- cogs_drift moves each entity's cost base differently, so margin actually varies by entity
  -- and by month. Flat demo data makes every summary sound the same.
  SELECT * FROM VALUES
    ('EMEA', 1.00,  0.00),
    ('AMER', 1.35,  0.07),
    ('APAC', 0.62, -0.05) AS t(entity, scale, cogs_drift)
),
accounts AS (
  SELECT * FROM VALUES
    ('Revenue', 1.000), ('COGS', 0.545), ('Opex', 0.268), ('Other', 0.021) AS t(account, share)
),
cost_centers AS (
  SELECT explode(array('Platform', 'Field', 'G&A')) AS cost_center
)
SELECT
  m.booked_on,
  e.entity,
  c.cost_center,
  a.account,
  CAST(
    ROUND(
      1650000 * e.scale * a.share
      * (1 + 0.021 * MONTHS_BETWEEN(m.booked_on, DATE'2025-09-01'))          -- gentle growth
      * (1 + 0.06 * SIN(MONTHS_BETWEEN(m.booked_on, DATE'2025-09-01') / 1.9)) -- seasonality
      * CASE c.cost_center WHEN 'Platform' THEN 0.52 WHEN 'Field' THEN 0.33 ELSE 0.15 END
      -- cost accounts drift per entity and over time; revenue does not
      * CASE WHEN a.account IN ('COGS', 'Opex')
             THEN 1 + e.cogs_drift + 0.045 * SIN(MONTHS_BETWEEN(m.booked_on, DATE'2025-09-01') / 2.5)
             ELSE 1 END,
      2)
  AS DECIMAL(18,2)) AS amount
FROM months m
CROSS JOIN entities e
CROSS JOIN accounts a
CROSS JOIN cost_centers c
