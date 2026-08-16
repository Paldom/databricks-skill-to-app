# report.yaml schema

The contract is a **repo convention defined by this skill**, not a Databricks product feature. It
holds only what the SQL files cannot express; everything the platform already has a format for
(parameter types, execution identity, metric-view bindings) stays in the platform's own files so
the app materialization is a copy, not a translation.

**Contents:** [Layout](#layout) · [Top level](#top-level) · [semantic_layer](#semantic_layer) ·
[params](#params) · [blocks](#blocks) · [guardrails](#guardrails) · [genie](#genie) ·
[Full example](#full-example) · [Versioning](#versioning)

## Layout

```
reports/<report-name>/
├── report.yaml                      # this file
├── queries/
│   ├── pnl_summary.sql              # runs as the service principal
│   ├── pnl_by_entity.obo.sql        # runs on behalf of the signed-in user
│   └── data_watermark.sql           # optional freshness probe
└── metric-views/
    └── definitions.json             # optional, AppKit's documented shape
```

Block key → file name: `key.sql`, or `key.obo.sql` when `identity: user`. The key is the
filename without `.sql` / `.obo.sql`, matching AppKit's queryKey rule.

## Top level

| Field | Required | Notes |
| --- | --- | --- |
| `version` | yes | semver of the contract itself. Bump major on any output-schema or parameter change that would break an existing consumer. |
| `name` | yes | kebab-case, matches the directory name. |
| `title` | yes | human title, rendered as the report heading. |
| `owner` | yes | who is accountable for the numbers. A report with no owner is not governed. |
| `semantic_layer` | no | see below |
| `params` | no | see below |
| `blocks` | yes | at least one |
| `guardrails` | yes | see below |
| `genie` | no | see below |

## semantic_layer

```yaml
semantic_layer:
  metric_views: metric-views/definitions.json   # path, relative to report.yaml
  allowed_catalogs: [main]                      # every relation in every query must sit here
```

`allowed_catalogs` is the blast radius. The validator rejects any three-part relation whose catalog
is not listed, which is what stops a "small edit" from silently reading production from a dev
report.

## params

Declares defaults and bounds. The **type** lives in the `.sql` file's `-- @param` annotation and
must agree with the type here.

```yaml
params:
  - name: start_date
    type: DATE
    default: "2026-01-01"
  - name: row_limit
    type: INT
    default: 50
    max: 500            # optional; enforced by the runner and the validator's literal-cap rule
  - name: entity
    type: STRING
    default: ""
    optional_with: entity_set    # companion boolean: see below
```

**Optional filters.** Prefer a companion boolean over a magic value:

```sql
-- @param entity STRING
-- @param entity_set BOOLEAN
WHERE (:entity_set = false OR entity = :entity)
```

A sentinel (`''`, `'1900-01-01'`) is only acceptable when the value is provably outside the
column's domain — otherwise "no filter" and "filter for this real value" become the same request.
AppKit rejects an empty string for a `DATE` parameter, so a date filter is either a companion
boolean or a proven sentinel date.

## blocks

```yaml
blocks:
  - key: pnl_summary
    kind: kpi                 # kpi | table | chart | narrative
    title: P&L at a glance
    identity: service_principal   # service_principal | user
    trust: certified              # certified | generated
```

| Field | Notes |
| --- | --- |
| `key` | must resolve to exactly one file in `queries/` |
| `kind` | drives rendering; `narrative` blocks are text the renderer summarizes, still backed by a query |
| `identity` | `user` → the file must be named `<key>.obo.sql`; the validator enforces the pairing |
| `trust` | `certified` = deterministic contract SQL. `generated` = produced by a model at run time (Genie). Never label a generated block certified. |

## guardrails

```yaml
guardrails:
  max_rows: 5000              # every query needs a literal or param-capped LIMIT at or below this
  require_total_order: true   # LIMIT without a sufficient ORDER BY fails validation
  freshness:
    watermark_block: data_watermark   # block whose first column is the max source timestamp
    max_lag: 26h                      # runner fails/flags the report beyond this
```

Freshness is a property of the **data**, not of when the query ran. An execution timestamp says
nothing about whether the source loaded today, which is why the watermark comes from a query.

## genie

```yaml
genie:
  space_id: 01ef0000000000000000000000000000
  trust: generated            # the only legal value
```

A Genie Agent's `example_question_sqls` *guide* SQL generation; they do not constrain it. Answers
from the Genie path are therefore a different trust class from the contract's certified queries and
must be labelled as generated wherever they surface. Keeping the space id here lets consumers offer
the natural-language path beside the certified blocks without pretending the two are equivalent.

## Full example

```yaml
version: 1.0.0
name: monthly-pnl
title: Monthly P&L
owner: finance-analytics@example.com

semantic_layer:
  metric_views: metric-views/definitions.json
  allowed_catalogs: [main]

params:
  - name: start_date
    type: DATE
    default: "2026-01-01"
  - name: end_date
    type: DATE
    default: "2026-01-31"
  - name: row_limit
    type: INT
    default: 50
    max: 500

blocks:
  - key: pnl_summary
    kind: kpi
    title: P&L at a glance
    identity: service_principal
    trust: certified
  - key: pnl_by_entity
    kind: table
    title: Result by entity
    identity: user
    trust: certified
  - key: data_watermark
    kind: narrative
    title: Data freshness
    identity: service_principal
    trust: certified

guardrails:
  max_rows: 5000
  require_total_order: true
  freshness:
    watermark_block: data_watermark
    max_lag: 26h

genie:
  space_id: 01ef0000000000000000000000000000
  trust: generated
```

## Versioning

The contract version is the thing consumers pin. Record it, plus the SHA-256 of every query file,
in whatever manifest a consumer materializes (see the `report-to-databricks-app` skill's drift
gate). A consumer that cannot state which contract version it is running is not governed — it is
just a copy of some SQL.

- **major** — a query is removed or renamed, an output column changes name/type/meaning, a
  parameter is added without a default, guardrails tighten in a way that changes results.
- **minor** — a block is added, an optional parameter is added, a comment or title changes.
- **patch** — a fix that provably does not change returned values.
