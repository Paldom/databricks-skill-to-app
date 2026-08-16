---
name: report-skill-builder
description: Generates a self-contained report skill from a validated governed report contract - materializing the trusted queries, a Statement Execution runner, and a shadcn-styled HTML report whose ECharts code ports to AppKit. Use when the user asks for a skill or slash command that produces a specific report. Not for authoring general-purpose skills, defining the contract, or building a Databricks App.
argument-hint: <contract path> [skill folder]
license: MIT
---

# Report skill builder

Turns a governed report contract into a skill someone can run by typing the report's name. The
generated skill is **self-contained as a skill**: it carries a materialized copy of the contract, a
hash manifest, a runner, a renderer and the design system, so it keeps working when copied into
another repo or installed alone. It never depends on this skill being present. (The rendered page
is a different question — its charts come from a pinned CDN.)

The model's job in the generated skill is deliberately small — pick parameters, write two-sentence
summaries, report failures. The numbers come from the contract.

## When to use

- The user wants a repeatable, named report ("a skill that generates the monthly P&L").
- A contract exists (or is about to) and needs a consumer on the skill side.
- A generated report reads badly and needs the bundled design system applied.

## When NOT to use

- **Authoring a general skill** → `add-skill` in this repo, or `skill-creator`. This one only emits
  report skills bound to a contract.
- **Defining or fixing the contract** → `governed-report-contract`. If the contract does not
  validate, stop and go there; do not generate around a broken contract.
- **Building the interactive app** → `report-to-databricks-app`.
- **Ad-hoc "just show me the numbers"** → `databricks-data-discovery`. A skill for a report nobody
  will run twice is overhead.

## Workflow

1. **Require a valid contract.** The generator refuses otherwise, and that refusal is the feature:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/../governed-report-contract/scripts/validate_contract.py" reports/<name>
   ```

   If the sibling skill is not installed, pass `--validator <path>` in the next step.

2. **Generate.** The output folder name becomes the skill name, so make it kebab-case and specific
   (`monthly-pnl`, not `report`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/new_report_skill.py" \
     --contract reports/monthly-pnl \
     --out .claude/skills/monthly-pnl
   ```

   It materializes `contract/`, writes `contract.manifest.json` (SHA-256 per file), copies the
   runner, renderer and design system, renders `SKILL.md`, and writes a starter `evals/evals.json`.

3. **Check what was generated.** The description is derived from the report title — read it and make
   it sound like something a user would actually type. Confirm it is a single line, 150–400 chars,
   and does not collide with a sibling report skill's triggers.

4. **Dry-run the report** against a real warehouse before handing it over:

   ```bash
   python3 .claude/skills/monthly-pnl/scripts/run_report.py \
     --contract .claude/skills/monthly-pnl/contract \
     --profile <PROFILE> --warehouse <WAREHOUSE_ID> --out /tmp/pnl.json
   python3 .claude/skills/monthly-pnl/scripts/render_report.py \
     --envelope /tmp/pnl.json --out /tmp/pnl.html
   ```

   Open the HTML. Charts need the network (ECharts from a pinned CDN); everything else
   renders without it and each chart slot says so when it cannot draw. If it reads like a document
   rather than a dashboard, that is a copy problem — see
   [references/design-system.md](references/design-system.md), not a CSS problem.

5. **Tune the evals, don't invent them.** The generated cases are shaped from the report title;
   replace the ones that do not sound like this team's vocabulary. Keep ≥8 should-trigger and ≥8
   should-not-trigger, and make sure a sibling report skill is among the negatives.

6. **Report what you built:** the skill path, the contract version it pinned, the blocks and their
   execution identities, and the one command that regenerates it.

## Output spec

```
<skill>/
├── SKILL.md                    # single-line description, contract version pinned
├── contract/                   # materialized report.yaml + queries/ + metric-views/
├── contract.manifest.json      # contract version + SHA-256 per file
├── scripts/run_report.py       # contract -> result envelope (binds params, attests principal)
├── scripts/render_report.py    # envelope -> an HTML report (shadcn cards + Recharts)
├── assets/report.css           # shadcn design tokens
├── assets/report-template.html # page skeleton (pinned CDN tags for the chart runtime)
├── assets/report-charts.js     # ECharts options — the same objects AppKit takes
└── evals/evals.json            # ≥8 / ≥8 / 3 cases, report-specific
```

The runner exits non-zero if any block failed; the renderer exits non-zero if the page contains a
failed block. All three scripts take `--selftest`: the runner replays a fake CLI to check the
endpoint, polling, chunk pagination, parameter rejection and drift; the renderer checks escaping,
numeric fidelity and failure states; the generator checks materialization, hashing and overlap.

## The result envelope

The interface between running and rendering. Keeping it explicit is what lets the same numbers be
re-rendered, diffed or shipped elsewhere without re-querying:

```json
{
  "contract": {"name": "monthly-pnl", "version": "1.0.0", "title": "Monthly P&L", "owner": "..."},
  "generated_at": "2026-08-14T12:00:00Z",
  "attested_principal": "someone@example.com",
  "params": {"start_date": "2026-01-01"},
  "freshness": {"watermark": "2026-08-14T02:15:00Z", "max_lag": "26h"},
  "blocks": [{"key": "pnl_summary", "kind": "kpi", "identity": "user", "trust": "certified",
              "status": "ok", "columns": [...], "rows": [[...]], "row_count": 1}]
}
```

`status` is one of `ok`, `empty`, `partial`, `error`. A `partial` block was truncated or exceeded
the contract's row cap — it renders with a flagged line, never silently.

## Gotchas

- **Never edit the generated `contract/`.** The runner re-hashes it and refuses to run on a drifted
  copy. Change the canonical contract, then re-generate.
- **Charts need the network; the numbers do not.** The chart runtime is CDN-pinned, so a plot can
  fail to draw while every figure, table and state still renders. That is why each chart slot ships
  a visible fallback instead of a blank box. If a report must plot offline, inline the runtime.
- **`animation: false` is not cosmetic.** An animated series is mid-flight when a screenshot,
  print or PDF is taken, so the plot comes out blank or half-drawn.
- **A generated skill must not import from this one.** It gets copied and installed alone; anything
  it needs is inside its own folder. That is why the template and scripts are assets, not shared code.
- **The generated description is a draft.** Derived text routes worse than a sentence written for
  how this team talks about the report.
- **Two report skills steal each other's triggers.** "the report" is not a trigger. Name the report
  in the description and add the sibling as a negative case.
- **Summaries are the only model-authored text, and the most quotable.** They must cite only values
  present in the envelope, stay under two sentences, and keep their AI attribution mark.
- **`--skip-validate` exists for the case where you already ran the validator.** Using it to get past
  a failing contract produces a skill that confidently returns wrong numbers.

## References

- [references/design-system.md](references/design-system.md) — tokens, typography, copy budgets, the
  AI-summary rules, chart and state requirements, and how to extend without forking.
- `scripts/new_report_skill.py` — the generator (`--selftest` proves materialization, hashing and
  drift detection still work).
- `assets/` — everything copied into the generated skill: runner, renderer, CSS, page template,
  `SKILL.md` template.
