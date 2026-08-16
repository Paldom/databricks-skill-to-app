-- The governed semantic layer: a Unity Catalog metric view.
-- Ratios live here, once, so every consumer re-aggregates them safely instead of each report
-- re-deriving margin from whatever grain it happened to query.
CREATE OR REPLACE VIEW demo.pnl_demo.pnl_metrics
WITH METRICS
LANGUAGE YAML
AS $$
version: 1.1
source: demo.pnl_demo.pnl_fact
comment: "Monthly P&L metrics for the databricks-skill-to-app worked example"
dimensions:
  - name: Booked Month
    expr: DATE_TRUNC('MONTH', booked_on)
    comment: "Month the posting was booked"
  - name: Entity
    expr: entity
    comment: "Reporting entity"
  - name: Cost Center
    expr: cost_center
  - name: Account
    expr: account
measures:
  - name: Revenue
    expr: SUM(CASE WHEN account = 'Revenue' THEN amount ELSE 0 END)
    comment: "Gross revenue"
  - name: COGS
    expr: SUM(CASE WHEN account = 'COGS' THEN amount ELSE 0 END)
  - name: Opex
    expr: SUM(CASE WHEN account = 'Opex' THEN amount ELSE 0 END)
  - name: Gross Profit
    expr: SUM(CASE WHEN account = 'Revenue' THEN amount WHEN account = 'COGS' THEN -amount ELSE 0 END)
  - name: Net Result
    expr: SUM(CASE WHEN account = 'Revenue' THEN amount ELSE -amount END)
  - name: Gross Margin Pct
    expr: 100 * SUM(CASE WHEN account = 'Revenue' THEN amount WHEN account = 'COGS' THEN -amount ELSE 0 END) / NULLIF(SUM(CASE WHEN account = 'Revenue' THEN amount ELSE 0 END), 0)
    comment: "Gross profit as a percentage of revenue - safe to re-aggregate at any grain"
$$
