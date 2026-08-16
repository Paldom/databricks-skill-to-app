# Dual-path portability profile

Why one `.sql` file can serve both a headless runner and a Databricks App — and the exact
constructs that break that promise.

**Contents:** [The two paths](#the-two-paths) · [Parameter annotations](#parameter-annotations) ·
[Supported `-- @param` types](#supported---param-types) · [LIMIT is INT](#limit-is-int) ·
[Execution identity](#execution-identity) · [Reserved parameters](#reserved-parameters) ·
[Sample values for type generation](#sample-values-for-type-generation) ·
[Type generation needs a warehouse](#type-generation-needs-a-warehouse) ·
[Metric views](#metric-views) · [Determinism](#determinism-limit-without-order-by-is-a-sample) ·
[Values arrive as strings](#values-arrive-as-strings) · [What does not port](#what-does-not-port) ·
[Conformance checklist](#conformance-checklist) · [Sources](#sources)

## The two paths

| | Headless runner (skill, script, CI) | Databricks App (AppKit) |
| --- | --- | --- |
| Transport | SQL Statement Execution API `POST /api/2.0/sql/statements/` | `POST /api/analytics/query/:query_key` (SSE) |
| SQL lives in | the contract's `queries/*.sql` | `config/queries/*.sql` (same bytes) |
| Parameter markers | `:name` | `:name` |
| Parameter binding | `"parameters": [{"name": "...", "value": "...", "type": "DATE"}]` | `sql.date("2026-01-01")` → `{__sql_type: "DATE", value: "2026-01-01"}` |

> **Endpoint, verified against a live workspace:** the execute path is
> `POST /api/2.0/sql/statements/`. `POST /api/2.0/sql/statements/execute` returns
> `No API found for 'POST /sql/statements/execute'` — the form used in the `databricks-metric-views`
> skill's CLI example. Statement status is `GET /api/2.0/sql/statements/<id>`, further result pages
> follow `result.next_chunk_internal_link`, and cancel is
> `POST /api/2.0/sql/statements/<id>/cancel`.

Both paths send **named markers plus a typed, string-encoded value**. Neither interpolates the
value into the SQL text. That is the whole basis of the shared core: the file is identical, only
the binding call differs. `type` is optional in the Statement Execution API and defaults to
`STRING` — never rely on that default; declare the type.

## Parameter annotations

AppKit reads optional type annotations from SQL comments. They are comments, so the same file
still executes unchanged through the Statement Execution API. Use them as the contract's
**canonical parameter declaration** — one file, no separate manifest to drift.

```sql
-- @param start_date DATE
-- @param end_date DATE
-- @param row_limit INT
SELECT entity, SUM(amount) AS amount
FROM main.finance.pnl_fact
WHERE booked_on BETWEEN :start_date AND :end_date
GROUP BY entity
ORDER BY amount DESC, entity
LIMIT :row_limit
```

## Supported `-- @param` types

Case-insensitive: `STRING`, `BOOLEAN`, `DATE`, `TIMESTAMP`, `BINARY`, `INT`, `BIGINT`, `TINYINT`,
`SMALLINT`, `FLOAT`, `DOUBLE`, `NUMERIC`, `DECIMAL`.

Binding helpers that exist today: `sql.string`, `sql.boolean`, `sql.date`, `sql.timestamp`,
`sql.binary`, `sql.number`, `sql.int`, `sql.bigint`, `sql.float`, `sql.double`, `sql.numeric`.

> The `databricks-apps` skill's `references/appkit/sql-queries.md` lists `sql.int()` and
> `sql.float()` as **not existing**. That list is stale — both are documented in the shipped
> AppKit docs. Confirm the current surface with
> `npx @databricks/appkit docs ./docs/api/appkit/Variable.sql.md` rather than trusting either
> document from memory.

Pass `DECIMAL`/`NUMERIC` values as **strings** (`sql.numeric("1234.56")`) so precision survives.

## LIMIT is INT

`LIMIT` and `OFFSET` require Spark `IntegerType`. A `BIGINT` parameter is rejected with
`INVALID_LIMIT_LIKE_EXPRESSION.DATA_TYPE`. Annotate row caps as `-- @param row_limit INT`.
`sql.number()` infers `INT` inside `[-2^31, 2^31-1]` and silently widens to `BIGINT` beyond it —
so a large value turns a working query into a runtime error. Annotate explicitly.

## Execution identity

Identity is set by **file name**, not by the call site:

| File | Runs as | Cache scope |
| --- | --- | --- |
| `<key>.sql` | the app service principal | shared across all users |
| `<key>.obo.sql` | the requesting user (on-behalf-of) | per user |

A block covered by Unity Catalog row filters or column masks must be `.obo.sql`, or every viewer
sees the service principal's slice of the data. The same query under two identities can
legitimately return different numbers — that is a governance decision, so record it in the
contract instead of leaving it to whoever names the file.

Metric views carry the same choice in `definitions.json` as
`"executor": "app_service_principal" | "user"` (default `app_service_principal`).

## Reserved parameters

`:workspaceId` is **injected by the server** and must **not** carry a `-- @param` annotation.
Annotating it makes the app try to bind it from the client.

## Sample values for type generation

Type generation runs `DESCRIBE QUERY` **without** binding real parameters, substituting a
placeholder per type (`''` for a string). Any query whose *shape* depends on a value therefore
fails to describe — most often a dynamic table name:
`IDENTIFIER('' || '.schema.table')` → `PARSE_SYNTAX_ERROR`.

Append a sample value, used only while describing:

```sql
-- @param target_catalog STRING = main
SELECT * FROM IDENTIFIER(:target_catalog || '.sales.nation')
```

String/`DATE`/`TIMESTAMP` samples are auto-quoted; numeric/`BOOLEAN`/`BINARY` samples are validated
against a strict literal shape, and anything that could inject SQL into the describe statement is
ignored in favour of the type placeholder.

Prefer avoiding `IDENTIFIER()` in a governed report entirely — a fully qualified, allowlisted
relation is both portable and auditable.

## Type generation needs a warehouse

`npx @databricks/appkit generate-types` requires `DATABRICKS_WAREHOUSE_ID` and connects to the
warehouse to infer result columns. It is **not** an offline SQL parser, which is why metric-view
SQL (`MEASURE(...)`, DBR 17.2+) types correctly — the warehouse compiles it.

Failure handling, which matters for CI:

- **Deterministic failures always crash the build** — SQL syntax errors, HTTP 404 (bad warehouse
  id), HTTP 400. Committed types must not hide a broken query.
- **Environmental failures fall back** to committed `shared/appkit-types/*.d.ts` with a loud stderr
  warning and exit 0 — auth failures, network, cold/deleted warehouse, `--wait` timeout.
- The generator never overwrites committed types with degraded `unknown` types.
- Commit the generated type files. For a metric-view app, `metric-views.d.ts` must already exist or
  the environmental fallback cannot satisfy the gate.

Use `--wait` in CI and production builds.

## Metric views

Declare the semantic layer once, in AppKit's documented shape, and keep the same file in the
contract:

```json
{
  "$schema": "https://databricks.github.io/appkit/schemas/metric-source.schema.json",
  "metricViews": {
    "pnl": { "source": "main.finance.pnl_metrics" },
    "entity_pnl": { "source": "main.finance.entity_metrics", "executor": "user" }
  }
}
```

The route `POST /api/analytics/metric/:key` takes a structured request
(`measures`, `dimensions`, `filter`, `timeGrain`/`timeDimension`, `orderBy`, `limit`) and builds
`SELECT MEASURE(...) ... GROUP BY ALL` itself, backtick-quoting the FQN and every identifier and
binding filter values as parameters (`:f_0`, `:f_1`, …). Limits: ≥1 and ≤50 measures, ≤20
dimensions, `limit` ≤ 100000, filter nesting ≤ 8, ≤ 100 children per group, ≤ 1000 values per
predicate. Measures and dimensions must be unique across both lists. The route is dormant — every
key returns `404` — until `definitions.json` exists.

**Order measures by their SELECT alias.** `ORDER BY MEASURE(\`revenue\`)` is rejected with
`METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT`; the generated SQL aliases each measure, so order by
`revenue`.

Hand-written metric-view SQL follows the same rules as any metric view: every measure wrapped in
`MEASURE()`, `SELECT *` unsupported, backticks around names containing spaces.

## Determinism: LIMIT without ORDER BY is a sample

`LIMIT` with no `ORDER BY` returns whichever rows Spark produced first — it varies with
partitioning, parallelism and cache state. A KPI card built on one can show a different number run
to run with nothing erroring. A governed report must order by enough columns to make the row set a
**total order**: order by the measure you mean, then append the remaining dimensions as
tie-breakers. (The metric route does this for you; hand-written SQL does not.)

## Values arrive as strings

Both paths move values as typed strings, and result payloads are no different: `DECIMAL` and large
`BIGINT` come back as JSON strings even when generated TypeScript types say `number`. Coerce
explicitly at the edge — `Number(row.amount)` in the app, `decimal.Decimal(...)` in a Python
runner. Never do arithmetic on the raw value, and never round-trip money through a JS `number`
above 2^53.

## What does not port

Reject these in a contract query; they either fail in one path or defeat governance:

- more than one statement, or anything that is not a single read-only `SELECT`/`WITH`
- DDL/DML (`CREATE`, `INSERT`, `MERGE`, `DROP`, `ALTER`, `COPY INTO`), `CALL`, `SET`, `USE`
- string interpolation of any kind (`${…}`, `%s`, f-strings, `'` + concatenation) — parameter
  markers bind values only, so an interpolated identifier or predicate is an injection hole
- parameters in identifier positions (table, column, sort direction) without the `IDENTIFIER()` +
  sample-value pattern
- unqualified relations that depend on a session default catalog/schema
- `SELECT *` — the output schema must be stable enough to type and render
- Arrow formats on the metric route (rejected there; only `JSON_ARRAY`)
- empty `{"and": []}` / `{"or": []}` filter groups (rejected with `400`)

## Conformance checklist

Before declaring a contract portable, execute the same file both ways and diff the normalized
rows. Cover: strings containing quotes/colons/Unicode, `DATE` and DST-boundary `TIMESTAMP`,
a repeated marker, `DECIMAL` at high precision, `BIGINT` above 2^53, `LIMIT` at 0/1/cap, an
optional filter in both its set and unset state, and a metric-view query using `MEASURE()` and a
dimension. Compare column names, types and values — not just row counts.

## Sources

- SQL Statement Execution API, named parameter markers and the `parameters` array —
  <https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial>
- AppKit analytics plugin (query files, `-- @param`, execution context, metric views, filters,
  determinism) — `npx @databricks/appkit docs ./docs/plugins/analytics.md`
- AppKit type generation (warehouse requirement, `--wait`, failure taxonomy, metric-view types) —
  `npx @databricks/appkit docs ./docs/development/type-generation.md`
- `sql.*` binding helpers — `npx @databricks/appkit docs ./docs/api/appkit/Variable.sql.md`
- Unity Catalog metric views (`WITH METRICS LANGUAGE YAML`, `MEASURE()`, DBR 17.2+) —
  <https://docs.databricks.com/en/metric-views/>
