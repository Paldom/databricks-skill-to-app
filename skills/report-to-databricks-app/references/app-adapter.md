# Contract → AppKit adapter

What this skill owns, what it delegates, and the AppKit specifics that decide whether the app
returns the contract's numbers or its own.

**Contents:** [Division of labour](#division-of-labour) · [Where the files land](#where-the-files-land) ·
[Scaffolding](#scaffolding) · [Type generation](#type-generation) ·
[Metric views in the app](#metric-views-in-the-app) · [Binding parameters](#binding-parameters) ·
[Execution identity and cache](#execution-identity-and-cache) · [The DAB](#the-dab) ·
[CI gate](#ci-gate) · [Verifying](#verifying) · [Sources](#sources)

## Division of labour

| Concern | Owner |
| --- | --- |
| Is the contract valid? | `governed-report-contract` |
| Scaffolding, plugins, `--features`, deploy mechanics | `databricks-apps` |
| Screen layout, KPI composition, charts, states, Genie trust | `databricks-app-design` |
| Bundle structure, targets, permissions | `databricks-dabs` |
| **Copying the contract in, binding metric views, gating drift** | **this skill** |

Doing any of the delegated work here duplicates a maintained skill and steals its triggers. Load
them alongside instead.

## Where the files land

```
apps/<app>/
├── config/queries/pnl_summary.sql        # copied byte-for-byte from the contract
├── config/queries/pnl_by_entity.obo.sql  # suffix preserved — it IS the identity declaration
├── config/metric-views/definitions.json  # copied from the contract
├── config/report.manifest.json           # contract version + SHA-256 per file (this skill)
└── shared/appkit-types/*.d.ts            # generated, committed
```

The query key is the filename without `.sql` / `.obo.sql`, so contract block keys become AppKit
query keys with no mapping table to keep in sync.

## Scaffolding

Owned by `databricks-apps` — read `databricks apps manifest` and let that skill build the
`databricks apps init` command. Only two choices are the adapter's business:

- **`--features analytics`.** A report is warehouse-backed aggregation, not sub-second lookup, so
  it does not need Lakebase. Adding a database to a read-only report is a decision to explain, not
  a default.
- **A metric-view report also needs `config/metric-views/definitions.json`**, which the
  materializer writes — the analytics feature alone leaves the metric route dormant.

## Type generation

Types come from the warehouse, not from parsing:

```bash
export DATABRICKS_WAREHOUSE_ID=<id>
npx @databricks/appkit generate-types --wait
```

- `--wait` in CI and production builds.
- **Commit** `shared/appkit-types/*.d.ts`. On a fresh CI checkout the generator tries the warehouse
  and falls back to the committed files only for *environmental* failures (auth, network, cold or
  deleted warehouse, timeout), emitting a loud stderr warning. *Deterministic* failures — SQL syntax
  errors, HTTP 404 on the warehouse id, HTTP 400 — always crash the build, which is what you want:
  committed types must never hide a broken query.
- For a metric-view app, `metric-views.d.ts` must already exist, or the environmental fallback
  cannot satisfy the gate. `analytics.d.ts` alone is not enough.
- The generator never overwrites committed types with degraded `unknown` types.

## Metric views in the app

`config/metric-views/definitions.json` activates `POST /api/analytics/metric/:key` and the typed
`useMetricView` hook. Without the file the route returns `404` for every key.

The route builds the SQL itself — `SELECT MEASURE(...) ... GROUP BY ALL` — backtick-quoting the FQN
and every identifier and binding filter values as parameters. So the app can offer measure and
dimension choices without hand-writing metric SQL, while the governed definition stays in Unity
Catalog. Request caps: 1–50 measures, ≤20 dimensions, `limit` ≤ 100000, filter depth ≤ 8.

Two rules that bite:

- **Order measures by their SELECT alias.** `ORDER BY MEASURE(\`revenue\`)` fails with
  `METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT`.
- **Always send `orderBy` with a `limit`.** The route appends the remaining dimensions as
  tie-breakers to produce a total order, so the same request returns the same rows.

## Binding parameters

The `-- @param` annotations already in the contract's SQL are what AppKit reads. On the client, bind
with the matching helper:

```ts
const { data } = useAnalyticsQuery("pnl_summary", {
  start_date: sql.date("2026-01-01"),
  row_limit: sql.int(50),
});
```

- `LIMIT`/`OFFSET` need `INT`; `sql.number()` widens to `BIGINT` past 2^31 and the query then fails
  with `INVALID_LIMIT_LIKE_EXPRESSION.DATA_TYPE`. Use `sql.int()` for row caps.
- Pass `DECIMAL`/`NUMERIC` as strings via `sql.numeric()`.
- `DECIMAL` and large `BIGINT` come back as **strings** even when the generated type says `number` —
  `Number(row.amount)` before any arithmetic or formatting.
- `:workspaceId` is server-injected and must not be annotated or bound.
- Verify the helper surface with `npx @databricks/appkit docs ./docs/api/appkit/Variable.sql.md`;
  the `databricks-apps` skill's SQL reference is stale on `sql.int()` / `sql.float()`.

## Execution identity and cache

| File | Runs as | Cache |
| --- | --- | --- |
| `<key>.sql` | app service principal | shared across all users |
| `<key>.obo.sql` | the requesting user | per user |

Metric views take `"executor": "app_service_principal" \| "user"` in `definitions.json`.

This is the single most consequential thing the adapter preserves. Renaming `x.obo.sql` to `x.sql`
silently converts a per-user, permission-respecting block into one query result served from a shared
cache to everyone — the same numbers for every viewer, computed as a principal that may see more
than they do. If the contract declares `identity: user`, the app file keeps the suffix.

## The DAB

Deploying the app through a bundle is what makes the whole thing reproducible. `databricks-dabs`
owns the details; the report-specific parts are:

- the app resource pointing at the app source path;
- `catalog`, `schema` and `warehouse_id` as bundle **variables**, so dev and prod targets bind
  different data without editing a single query — the contract's `allowed_catalogs` still bounds
  what is legal;
- the contract directory included in the bundle sources so a deploy carries the SQL it was built
  from.

Validate with `databricks bundle validate --strict --target <target>` before deploying.

## CI gate

```bash
python3 scripts/materialize_app.py --contract reports/<name> --app apps/<app> --check
```

Exit 1 on any of: a hand-edited app copy, a stale copy after the contract moved, a contract version
bump the app has not taken, a query dropped from the contract but still in the app, or a query file
present in `config/queries/` that the contract does not govern.

Run it in CI next to `generate-types --wait`. Together they mean the app's **managed query files**
match the contract at build time. They do not prove the deployed app only ever executes that SQL —
an environmental type-generation failure can still fall back to committed types, and neither check
sees SQL embedded in server code. Keep queries in `config/queries/` and the gate stays meaningful.

## Verifying

Validation, deployment and smoke-test mechanics belong to `databricks-apps` (including updating
`tests/smoke.spec.ts` selectors, which the template's defaults will fail, and keeping smoke-test
result sets under the 1 MB analytics payload cap). Always pass `--profile`.

The adapter's own verification is **parity**: run the report skill and the app with the same
parameters and the same identity and compare a KPI per block. Treat it as a smoke check — it does
not cover every parameter combination, filter or cache state. Record, alongside the numbers: the
contract version, the manifest digest, the attested principal, the warehouse, and the watermark.
Different numbers with an in-sync manifest point at identity or freshness before SQL.

## Sources

- AppKit analytics plugin (query files, execution context, metric views, filters, determinism) —
  `npx @databricks/appkit docs ./docs/plugins/analytics.md`
- AppKit type generation (warehouse requirement, `--wait`, failure taxonomy, metric-view types) —
  `npx @databricks/appkit docs ./docs/development/type-generation.md`
- `sql.*` binding helpers — `npx @databricks/appkit docs ./docs/api/appkit/Variable.sql.md`
- Databricks Apps platform and CLI — the `databricks-apps` skill and
  <https://docs.databricks.com/dev-tools/databricks-apps/>
- Bundles — <https://docs.databricks.com/dev-tools/bundles/>
