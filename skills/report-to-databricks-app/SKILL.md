---
name: report-to-databricks-app
description: Materializes a validated governed report contract into a Databricks AppKit app - copying its trusted queries into config/queries, binding metric views, and gating drift with a hash manifest. Use when a report skill or contract should become an app, or the app's SQL has drifted from the report. Not for scaffolding an unrelated app, app UI design, or bundle deployment.
argument-hint: <contract path> [app path]
---

# Report contract → Databricks App

The second consumer of the governed reporting core. A report skill renders the contract offline;
this makes the same contract interactive on Databricks Apps — the same SQL files, the same
parameters, the same execution identities.

This skill is an **adapter, not an app builder**. It owns exactly one thing: getting the contract
into the app correctly and keeping it there. Scaffolding, screen design and deployment belong to
skills that already do them well, and this skill loads them rather than reimplementing them.

## When to use

- A validated contract (or a report skill built from one) should become an interactive app.
- The app and the report disagree on a number.
- Someone edited the app's SQL by hand and it needs to go back under the contract.
- CI should fail when the app's copy drifts.

## When NOT to use

- **An app with no contract behind it** → `databricks-apps`. This skill has nothing to materialize.
- **How the screens should look** → `databricks-app-design` (KPI composition, charts, states,
  Genie trust). Load it alongside; do not design here.
- **Bundle structure, targets, permissions** → `databricks-dabs`.
- **A managed dashboard** → `databricks-aibi-dashboards`. "Build me a dashboard" is not this skill.
- **Generating the report skill** → `report-skill-builder`.

## Workflow

1. **Validate the contract first.** An app built on an invalid contract is a faster way to be wrong.

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../governed-report-contract/scripts/validate_contract.py" reports/<name>
   ```

2. **Load the delegated skills.** `databricks-core` for auth and profile selection, `databricks-apps`
   for scaffolding, `databricks-app-design` because this app displays data. Ask the user for the
   profile; never auto-select one.

3. **Scaffold or locate the app.** Read the manifest before building the init command — plugin keys
   and resource fields come from it, not from memory:

   ```bash
   databricks apps manifest --profile <PROFILE>
   databricks apps init --name <name> --features analytics \
     --set analytics.sql-warehouse.id=<WAREHOUSE_ID> --run none --profile <PROFILE>
   ```

4. **Materialize the contract. This is the skill's actual job.**

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/materialize_app.py" \
     --contract reports/<name> --app apps/<app>
   ```

   Queries are copied byte-for-byte into `config/queries/` with the `.obo.sql` suffix intact,
   `metric-views/definitions.json` is bound, and `config/report.manifest.json` records the contract
   version and a SHA-256 per file. Queries dropped from the contract are removed from the app.

5. **Generate types from the warehouse**, then commit them:

   ```bash
   export DATABRICKS_WAREHOUSE_ID=<id>
   npx @databricks/appkit generate-types --wait
   ```

6. **Build the screens with `databricks-app-design`**, binding every element to a `queryKey` from
   the contract or a `useMetricView` key. Do not add a query in the app: a `SELECT` that exists only
   in `App.tsx` is exactly the drift this whole design prevents.

7. **Gate drift in CI:**

   ```bash
   python3 scripts/materialize_app.py --contract reports/<name> --app apps/<app> --check
   ```

8. **Validate, deploy, verify** — mechanics per `databricks-apps`:

   ```bash
   databricks apps validate --profile <PROFILE>
   databricks apps get <app-name> --profile <PROFILE> -o json   # app_status.state, url
   ```

9. **Smoke-check parity.** Run the report skill and the app with the same parameters and the same
   identity and compare a KPI from each block. This is a smoke check, not a proof: it does not
   cover every parameter combination, filter, or cache state. Record the contract version, the
   manifest digest, the attested principal and the watermark alongside the numbers.

## Output spec

- `apps/<app>/config/queries/*.sql` — byte-identical to the contract, suffixes preserved
- `apps/<app>/config/metric-views/definitions.json` — bound semantic layer
- `apps/<app>/config/report.manifest.json` — contract version + per-file SHA-256
- `shared/appkit-types/*.d.ts` — generated **and committed**
- A drift check wired into CI, exiting non-zero
- A stated parity result: same contract version, same numbers, or a named reason why not

## Gotchas

- **Renaming `x.obo.sql` to `x.sql` is a data-exposure change, not a tidy-up.** It converts a
  per-user query into one shared, service-principal result cached for everyone. The materializer
  preserves the suffix; never "simplify" it afterwards.
- **Editing `config/queries/` by hand is the drift.** It is a generated directory. Change the
  contract and re-materialize; `--check` exists to make the hand-edit fail loudly.
- **An *added* query is worse than an edited one.** `config/queries/` is runnable in full, so a
  hand-added `x.sql` is ungoverned SQL executing as the service principal — including a copy of an
  OBO block with its per-user isolation removed. `--check` compares what the app can actually run
  against the contract, not just what the manifest lists, and re-materializing deletes the extra.
- **`x.sql` and `x.obo.sql` are the same query key.** Shipping both makes one block claim two
  execution identities; the materializer refuses rather than letting AppKit pick.
- **`useMetricView` needs `definitions.json` to exist.** Without it every metric key returns `404`,
  which looks like a routing bug and is a config one.
- **Order metric measures by their SELECT alias**, not `MEASURE(...)`, and always pair `limit` with
  `orderBy` — otherwise the card shows a different row set run to run.
- **`LIMIT` bound with `sql.number()` breaks past 2^31** by widening to `BIGINT`. Use `sql.int()`.
- **`DECIMAL` and big `BIGINT` arrive as strings** despite the generated types. Coerce before
  formatting, or the app quietly renders `NaN` where the report shows a number.
- **Committed types are a CI fallback, not a substitute.** A missing `metric-views.d.ts` turns a
  cold warehouse into a failed build.
- **Different numbers with an in-sync manifest** means execution identity or freshness — check the
  attested principal and the watermark before suspecting the SQL.

## References

- [references/app-adapter.md](references/app-adapter.md) — the full division of labour, AppKit type
  generation and its failure taxonomy, metric-view routing rules, parameter binding, the DAB
  variables that make dev/prod reproducible, and the CI gate.
- `scripts/materialize_app.py` — copy + manifest + `--check` drift gate (`--selftest` proves it
  catches hand-edits, stale copies, version bumps and removals).
