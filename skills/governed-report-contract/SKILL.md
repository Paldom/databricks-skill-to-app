---
name: governed-report-contract
description: Defines and validates a versioned governed report contract - report.yaml plus parametric trusted queries and metric-view bindings - so a report returns the same numbers in a skill, a script and a Databricks App. Use when the user wants trusted, pinned or reproducible report queries, or to validate one. Not for creating metric views, Genie agents, AI/BI dashboards or app scaffolding.
argument-hint: <report name or contract path>
license: MIT
---

# Governed report contract

A report becomes ungovernable the moment its SQL lives inside whatever is rendering it. The skill
gets one version of the numbers, the app gets another, and nobody can say which is right. This
skill makes the SQL the artifact: one versioned directory of parametric, read-only queries over a
Unity Catalog semantic layer, validated by a script, that every consumer reads instead of inventing
its own.

**What the contract does and does not promise.** It guarantees that every consumer runs the *same
reviewed query definition* with the *same declared parameters and execution identity*. It does not
freeze the data or the semantic layer: the metric view can be redefined and the source can load
between two blocks. That is why the contract carries a freshness watermark and per-block execution
metadata instead of claiming a report-wide as-of time. `trust: certified` means "deterministic SQL
the owner reviewed" — it is a review status, not an attestation.

The contract is a convention defined here, not a Databricks product feature. It deliberately reuses
the platform's own file formats — AppKit `-- @param` annotations, the `.obo.sql` naming rule,
AppKit's `metric-views/definitions.json` — so materializing it into an app is a **copy, not a
translation**.

## When to use

- The user wants trusted / certified / pinned queries behind a report.
- The same numbers must appear in more than one place (a skill, a scheduled script, an app).
- A report's numbers moved and nobody can explain why.
- The user asks to validate, review or version a report contract.

## When NOT to use

- **Creating the metric view itself** → `databricks-metric-views`. This skill *binds* an existing
  view; it does not author the YAML metric definition.
- **Creating or tuning a Genie Agent** → `databricks-genie-agents`.
- **Building a dashboard** → `databricks-aibi-dashboards` (managed AI/BI) — a plain "build me a
  dashboard" is never this skill.
- **Scaffolding or deploying an app** → `databricks-apps`, and `report-to-databricks-app` for the
  contract-to-app adapter.
- **Generating the report skill** → `report-skill-builder`.
- One throwaway query. A contract for a query nobody will run twice is pure overhead.

## Workflow

1. **Load the parent skill.** `databricks-core` for CLI auth and profile selection. Never
   auto-select a profile — pass `--profile <name>` and let the user choose.

2. **Find the semantic layer before writing SQL.** Prefer an existing metric view; report SQL that
   reaches past a governed view into raw fact tables re-implements business logic that already has
   an owner.

   ```bash
   databricks experimental aitools tools query --profile <PROFILE> --warehouse <WH> \
     "SHOW VIEWS IN main.finance"
   # Confirm the object really is a metric view — a metric view's DDL contains
   # `WITH METRICS LANGUAGE YAML`; a plain view's does not.
   databricks experimental aitools tools query --profile <PROFILE> --warehouse <WH> \
     "SHOW CREATE TABLE main.finance.pnl_metrics"
   databricks experimental aitools tools discover-schema --profile <PROFILE> main.finance.pnl_metrics
   ```

   If no metric view exists and the user wants one, stop and route to `databricks-metric-views`,
   then come back.

3. **Scaffold the contract directory.**

   ```
   reports/<name>/
   ├── report.yaml
   ├── queries/
   └── metric-views/definitions.json   # optional
   ```

   Full field reference: [references/contract-schema.md](references/contract-schema.md).

4. **Write one query per block**, each a single read-only `SELECT`/`WITH`, fully qualified, with
   every parameter declared in the file itself:

   ```sql
   -- @param start_date DATE
   -- @param row_limit INT
   SELECT entity, MEASURE(`Net Result`) AS net_result
   FROM main.finance.pnl_metrics
   WHERE `Booked Month` >= :start_date
   GROUP BY ALL
   ORDER BY net_result DESC, entity
   LIMIT :row_limit
   ```

   Decide **execution identity per block**, and let the filename carry it: `<key>.sql` runs as the
   app service principal with a shared cache; `<key>.obo.sql` runs on behalf of the signed-in user
   with a per-user cache. Any block behind a Unity Catalog row filter or column mask must be
   `.obo.sql`.

5. **Validate. This is the gate.**

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/validate_contract.py" reports/<name>
   ```

   Exit 0 = valid, 1 = errors, 2 = cannot run (PyYAML missing, or bad path). Fix the SQL or the
   YAML — never weaken the validator. `--selftest` proves the rules still fire.

6. **Execute each block once, under the identity it declares.** Build the request with a JSON
   encoder — never paste SQL into a shell-quoted JSON string, because the first apostrophe in a
   comment breaks the command and the second one changes it:

   ```bash
   python3 - <<'PY' > /tmp/req.json
   import json, sys
   sql = open("reports/<name>/queries/pnl_summary.sql").read()
   json.dump({"warehouse_id": "<WH>", "statement": sql,
              "format": "JSON_ARRAY", "disposition": "INLINE",
              "wait_timeout": "30s", "on_wait_timeout": "CONTINUE",
              "parameters": [{"name": "start_date", "value": "2026-01-01", "type": "DATE"},
                             {"name": "row_limit", "value": "50", "type": "INT"}]}, sys.stdout)
   PY
   databricks api post /api/2.0/sql/statements/ --profile <PROFILE> --json @/tmp/req.json
   ```

   The path is `/api/2.0/sql/statements/` — **not** `/api/2.0/sql/statements/execute`, which returns
   `No API found for 'POST /sql/statements/execute'`. (The `databricks-metric-views` skill's CLI
   example uses the `/execute` form; it does not work.)

   Poll `GET /api/2.0/sql/statements/<id>` until `status.state` is terminal, then follow
   `result.next_chunk_internal_link` until it is absent — stopping at the first chunk silently
   drops rows. Then **attest the
   principal** rather than assuming it — the Statement Execution API runs as whatever credential
   the profile carries, so a block declared `identity: user` is only really running as that user
   when the caller is that user:

   ```bash
   databricks api post /api/2.0/sql/statements/execute --profile <PROFILE> \
     --json '{"warehouse_id": "<WH>", "statement": "SELECT current_user() AS principal"}'
   ```

7. **Record evidence, not data.** Keep the statement id, the returned column schema, the row count,
   the watermark and the contract version. Do not commit result rows — a governed report's output
   is exactly the data the governance exists to protect.

8. **Report what is governed.** State the contract version, each block's trust class and execution
   identity, the attested principal, and the freshness watermark. Anything the user can't see, they
   can't trust.

## Output spec

- `reports/<name>/report.yaml` — version, owner, params, blocks, guardrails
- `reports/<name>/queries/*.sql` — one read-only parametric query per block, `-- @param` annotated
- `reports/<name>/metric-views/definitions.json` — optional semantic-layer binding
- `validate_contract.py` exits 0
- A one-paragraph summary naming the contract version and each block's identity + trust class

## Failure modes

- **`LIMIT` without `ORDER BY`.** Returns whichever rows Spark produced first — the number changes
  between runs with nothing erroring. Order by the measure, then append the remaining dimensions as
  tie-breakers.
- **A `BIGINT` row cap.** `LIMIT`/`OFFSET` need `IntegerType`; a `BIGINT` parameter fails with
  `INVALID_LIMIT_LIKE_EXPRESSION.DATA_TYPE`. Annotate `INT`.
- **`ORDER BY MEASURE(...)`.** Rejected with `METRIC_VIEW_INVALID_MEASURE_FUNCTION_INPUT` — order by
  the SELECT alias instead.
- **String interpolation "just for the filter".** Markers bind *values*; a relation name needs
  `IDENTIFIER(:param)`, which this contract prohibits — a governed query names its objects so they
  can be audited and permission-checked. Interpolating either is an injection hole, and the
  validator fails the build.
- **A shared cache over identity-dependent SQL.** `<key>.sql` results are cached across every user.
  If the query calls `current_user()`, `is_member()`, or reads a dynamic view, one user's slice gets
  served to everyone. Such a block must be `identity: user` / `.obo.sql`; the validator enforces it.
- **Assuming the filename enforces identity.** It does inside AppKit. In a headless run the query
  executes as whatever credential the CLI profile holds, so attest with `SELECT current_user()`
  instead of trusting the suffix.
- **The semantic layer moving under the contract.** `CREATE OR REPLACE VIEW ... WITH METRICS` can
  redefine a measure without touching a single contract file. Pin the metric view's owner in
  `report.yaml`, and treat a definition change as a major version bump of the report.
- **`max_rows` mistaken for a cost control.** It caps what comes back, not what gets scanned. Use it
  for payload safety and rely on warehouse limits and query profiles for cost.
- **Sentinel values for optional filters.** `''` and `'1900-01-01'` make "no filter" and "filter for
  this real value" the same request. Use a companion boolean unless the sentinel is provably outside
  the column's domain.
- **Treating Genie output as certified.** A Genie Agent's `example_question_sqls` guide SQL
  generation; they do not constrain it. Genie answers are a separate trust class — label them
  generated, always.
- **Claiming a report-wide as-of time.** Blocks execute independently, so a wall-clock timestamp
  says nothing about the data. Freshness comes from a watermark query against the source.
- **Unqualified relations.** They resolve against whatever default catalog the session happens to
  have, which is how a dev report quietly reads production.
- **DECIMAL and big BIGINT arrive as strings** in both paths, even when generated types say
  `number`. Coerce at the edge; never round-trip money through a float.

## References

- [references/portability.md](references/portability.md) — why one `.sql` file survives both
  execution paths, the verified `-- @param` and `sql.*` surfaces, metric-view routing, and the
  constructs that do not port.
- [references/contract-schema.md](references/contract-schema.md) — every `report.yaml` field, the
  optional-filter patterns, and the versioning rules consumers pin against.
- `scripts/validate_contract.py` — the deterministic gate. Run it before claiming a contract is done.
