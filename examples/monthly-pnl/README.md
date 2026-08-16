# Worked example: Monthly P&L

One governed contract, two consumers, the same numbers. Everything here was built and run against
a real workspace — the sample report and result envelope are actual output, not mock-ups.

```
examples/monthly-pnl/
├── databricks.yml                  # DAB: recreates the semantic layer + the app
├── setup/                          # synthetic P&L fact table + the metric view
├── reports/monthly-pnl/            # THE CONTRACT - the single definition of the numbers
│   ├── report.yaml
│   ├── queries/*.sql               # parametric, -- @param annotated, read-only
│   └── metric-views/definitions.json
├── generated-skill/monthly-pnl/    # consumer 1: produced by report-skill-builder
├── app/config/                     # consumer 2: produced by report-to-databricks-app
├── report-1080p.css                # example-only: locks the sheet to one 1920x1080 screen
├── report-sample.html              # real rendered output
└── run-sample.json                 # the result envelope it was rendered from
```

## Recreate it

Pick your own profile — nothing here selects one for you.

```bash
PROFILE=<your-profile>
WAREHOUSE=<your-warehouse-id>
```

**1. The semantic layer.** Either run the deploy script directly:

```bash
python3 examples/monthly-pnl/setup/deploy_semantic_layer.py --profile "$PROFILE" --warehouse "$WAREHOUSE"
```

or deploy the bundle, which creates the same objects as a job plus the app resource:

```bash
cd examples/monthly-pnl
databricks bundle validate --strict --target dev --profile "$PROFILE" --var="warehouse_id=$WAREHOUSE"
databricks bundle deploy --target dev --profile "$PROFILE" --var="warehouse_id=$WAREHOUSE"
databricks bundle run build_semantic_layer --target dev --profile "$PROFILE" --var="warehouse_id=$WAREHOUSE"
```

This creates `demo.pnl_demo.pnl_fact` (12 months × 3 entities × 3 cost centres × 4 accounts) and
the `demo.pnl_demo.pnl_metrics` metric view that owns every ratio.

**2. Validate the contract.** This gate runs before anything consumes it:

```bash
python3 skills/governed-report-contract/scripts/validate_contract.py examples/monthly-pnl/reports/monthly-pnl
```

**3. Consumer one — the report skill:**

```bash
python3 skills/report-skill-builder/scripts/new_report_skill.py \
  --contract examples/monthly-pnl/reports/monthly-pnl \
  --out examples/monthly-pnl/generated-skill/monthly-pnl --force

python3 examples/monthly-pnl/generated-skill/monthly-pnl/scripts/run_report.py \
  --contract examples/monthly-pnl/generated-skill/monthly-pnl/contract \
  --profile "$PROFILE" --warehouse "$WAREHOUSE" --out /tmp/pnl.json

python3 examples/monthly-pnl/generated-skill/monthly-pnl/scripts/render_report.py \
  --envelope /tmp/pnl.json --out /tmp/pnl.html \
  --extra-css examples/monthly-pnl/report-1080p.css \
  --summary margin_trend="Margin held inside a 4.8 point band all year, bottoming in January before seven straight months of recovery." \
  --summary pnl_by_entity="AMER books the most revenue on the thinnest margin; APAC earns the widest margin on the smallest book."
```

`report-1080p.css` is example-only: it locks the sheet to a single 1920x1080 screen. The skill's
own stylesheet is a flowing document that works at any width, so the viewport decision lives here
and is layered on with `--extra-css`.

**4. Consumer two — the app:**

```bash
python3 skills/report-to-databricks-app/scripts/materialize_app.py \
  --contract examples/monthly-pnl/reports/monthly-pnl --app examples/monthly-pnl/app
```

Scaffold the AppKit project itself with the `databricks-apps` skill
(`databricks apps manifest` → `databricks apps init --features analytics`), point
`source_code_path` at it, then `npx @databricks/appkit generate-types --wait` and build the screens
with `databricks-app-design`.

## What the example is actually demonstrating

- **The ratio lives in the metric view.** `Gross Margin Pct` is defined once, so the grand total
  (43.8%) is computed from summed components rather than averaging twelve monthly percentages.
- **Rounding is the query's job.** The renderer never rounds — it would be changing the numbers.
  The contract's SQL rounds explicitly, so every consumer shows identical digits.
- **`ORDER BY` names the SELECT alias.** `ORDER BY MEASURE(...)` fails with
  `METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT`; the tie-breaker column is what makes `LIMIT`
  deterministic rather than an arbitrary sample.
- **A narrow-range ratio gets re-based, not truncated.** Twelve months of margin between 41.9 and
  46.7 are indistinguishable as zero-based bars. The chart plots each month against the period
  average, states that baseline on the chart, and labels every point with its actual value and
  signed difference — a declared re-basing rather than a silent axis break.
- **Freshness is a rail, not a field.** The header spine shows one segment per month with the
  covered run filled, so a source that stopped loading halfway is visible at a glance. Full
  provenance (contract version, principal, per-block status) rides along as an HTML comment.
- **The catalog is not a bundle variable.** Repointing a report at different data is a contract
  change, not a deploy flag — `semantic_layer.allowed_catalogs` bounds it and the validator enforces it.
- **Ungoverned SQL fails the build.** Drop any `.sql` into the app's `config/queries/` and
  `materialize_app.py --check` exits non-zero: it compares what the app can *run* against the
  contract, not just what its manifest lists.

## Caveats

- The data is synthetic and deterministic. It is not a real P&L and the entity names are invented.
- `report-sample.html` and `run-sample.json` are committed as illustration. Real report output is a
  data extract — keep it out of version control.
- The example deploys into the `demo` catalog. Change it and you must change the contract's
  `allowed_catalogs` too; that is the design working, not fighting you.
