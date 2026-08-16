# Setup prompt

A paste-ready `/goal` that runs the three skills end to end: **contract → skill → app**, one
governed reporting core with two consumers.

## Before you paste

- `databricks` CLI authenticated, and you know **which profile** to use — the goal never picks one
  for you (`databricks auth profiles`).
- A SQL warehouse id, and a Unity Catalog metric view (or the tables to build one from).
- `python3` with PyYAML (`pip install pyyaml`), and Node available for AppKit's `npx`.
- The three skills installed: `npx skills add Paldom/databricks-skill-to-app`.

Replace the five `<...>` placeholders on the first line before sending.

## The goal

```
Build one governed reporting core and both of its consumers for <REPORT NAME> on profile <PROFILE>, warehouse <WAREHOUSE_ID>, metric view <CATALOG>.<SCHEMA>.<VIEW>, owner <OWNER EMAIL>. Work autonomously. NEVER run git commit or git push - leave everything in the working tree for me to review.

Order is fixed; each stage is gated by the previous stage's verifier.

1. CONTRACT (/governed-report-contract). Confirm the object really is a metric view (SHOW CREATE TABLE shows WITH METRICS LANGUAGE YAML). Author reports/<REPORT NAME>/ with report.yaml, queries/*.sql and, if the semantic layer is bound, metric-views/definitions.json. Every parameter declared with `-- @param NAME TYPE` inside the .sql file. Every relation three-part qualified and inside semantic_layer.allowed_catalogs. Any block whose rows depend on the viewer gets identity: user and the .obo.sql suffix.
   GATE A: the skill's validate_contract.py exits 0 on reports/<REPORT NAME>. Fix the SQL or the YAML - never weaken the validator.
   GATE B: each query runs once via POST /api/2.0/sql/statements/ (NOT /execute, which 404s), built with a JSON encoder rather than shell-quoted SQL. Record statement id, column schema, row count and the attested current_user() - not the result rows.

2. SKILL (/report-skill-builder). Generate into .claude/skills/<REPORT NAME> from the validated contract. Rewrite the generated description so it sounds like something we would actually type, single line, 150-400 chars, and add the sibling report skills as should_not_trigger cases.
   GATE C: run_report.py --selftest, render_report.py --selftest and new_report_skill.py --selftest all exit 0; then a real run against <WAREHOUSE_ID> produces the HTML. Open it: every block leads with a number, no paragraph anywhere, no external requests, failed or partial blocks visible rather than dropped.

3. APP (/report-to-databricks-app). Load databricks-apps for scaffolding and databricks-app-design for the screens - do not design or scaffold inside the adapter. Materialize the SAME contract into the app, keeping .obo.sql suffixes. Generate types with `npx @databricks/appkit generate-types --wait` and commit them. Bind every UI element to a contract queryKey or metric key; a SELECT that exists only in App.tsx is the drift this whole design prevents.
   GATE D: materialize_app.py --check exits 0, `databricks apps validate --profile <PROFILE>` passes, and CI runs the --check.

4. PARITY. Run the skill and the app with the same parameters and the same identity, compare one KPI, and report the contract version both are on. Different numbers with an in-sync manifest means identity or freshness - check the attested principal and the watermark before suspecting the SQL.

Parallelism: only after GATE A, and only on disjoint files - one agent under .claude/skills/<REPORT NAME>/, one under apps/. Never two agents in the same directory.

Done when: gates A-D all pass, parity is reported with a number, and nothing was committed. Tell me exactly what changed and which gate proved each claim.
```

## Why it is shaped this way

- **Gates, not vibes.** Each stage ends in a command that exits non-zero, so "done" is checkable
  rather than asserted. Gate B brackets the contract with real execution because a contract that
  validates statically can still fail on the warehouse.
- **Ordering is load-bearing.** The app materializes the contract; generating it before Gate A
  produces an app that confidently serves wrong numbers.
- **Parallelism is restricted to disjoint directories.** The skill and the app are independent
  consumers, but both read the contract — so they may only run concurrently after it is frozen.
- **No git.** Every skill in this repo leaves changes in the working tree for review.
