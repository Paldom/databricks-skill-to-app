---
name: monthly-pnl
description: "Generates the Monthly P&L report as a self-contained HTML page from the pinned monthly-pnl contract v1.0.0, with the numbers traceable to its trusted queries. Use when the user asks for the Monthly P&L, this period's figures, or to re-run or refresh that report. Not for changing the report definition, other reports, dashboards or app deployment."
argument-hint: [period or parameter overrides]
---

# Monthly P&L

Produces the Monthly P&L as one self-contained HTML page from the pinned
`monthly-pnl` contract (v1.0.0, owned by finance-analytics@example.com). The SQL is not written here — it is
materialized from the contract under `contract/` and hash-pinned in `contract.manifest.json`, so
this skill and any app built from the same contract run the identical queries.

## When to use

- The user asks for the Monthly P&L, or for this period's numbers from it.
- The user wants the report refreshed, re-run, or exported for a different period.

## When NOT to use

- **Changing what the report measures** — edit the canonical contract and re-generate this skill.
  Editing `contract/` here breaks the hash manifest and the runner refuses to start.
- **A different report** → that report's own skill.
- **An interactive app** → the contract's AppKit app.
- **Ad-hoc data questions** → `databricks-data-discovery`.

## Workflow

1. **Pick the profile.** Never auto-select one; ask if it is not already established.

2. **Run the contract.** Parameters default to the contract's values; override only what the user
   asked for.

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/run_report.py" \
     --contract "${CLAUDE_SKILL_DIR}/contract" \
     --profile <PROFILE> --warehouse <WAREHOUSE_ID> \
     --param start_date=2025-09-01 \
     --param end_date=2026-08-01 \
     --param row_limit=10 \
     --out /tmp/monthly-pnl.json
   ```

   A non-zero exit means at least one block failed. Read the envelope: failed blocks carry their
   error and are rendered as failures rather than dropped.

3. **Write the summaries.** This is the judgment part, and the only part a model should author.
   For each block worth commenting on, write **at most two sentences** that a reader could not get
   from the number itself — a direction, a driver, a threshold crossed. Every claim must be
   supported by a value present in the envelope. No number appears in a summary unless it appears
   in that block's rows.

4. **Render.**

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/scripts/render_report.py" \
     --envelope /tmp/monthly-pnl.json --out /tmp/monthly-pnl.html \
     --summary <block_key>="<your two sentences>"
   ```

5. **Hand it over.** Give the user the file path, the contract version, the data watermark, and the
   attested principal the queries ran as. If any block is `partial` or `error`, say so first.

## The report

| Block | Kind | Runs as | Trust |
| --- | --- | --- | --- |
| `pnl_summary` | kpi | service_principal | certified |
| `margin_trend` | chart | service_principal | certified |
| `pnl_by_entity` | table | service_principal | certified |
| `data_watermark` | narrative | service_principal | certified |

| Parameter | Type | Default |
| --- | --- | --- |
| `start_date` | DATE | `2025-09-01` |
| `end_date` | DATE | `2026-08-01` |
| `row_limit` | INT | `10` |

## Output spec

- `/tmp/monthly-pnl.html` — one self-contained file: no CDN, no external fonts, prints correctly
- Every figure traceable to a block; every AI summary labelled and carrying its provenance
- A closing note naming the contract version, watermark, principal, and any degraded block

## Gotchas

- **Do not edit `contract/`.** It is a materialized copy. The runner re-hashes it against
  `contract.manifest.json` and refuses to run on a drifted copy — fix the canonical contract and
  re-generate.
- **Do not add numbers in prose that are not in the data.** A summary that computes its own
  percentage is a fabrication with a trustworthy font.
- **`identity: user` blocks run as whoever holds the profile credential here.** The envelope
  records the attested principal — quote it rather than assuming.
- **A stale watermark is a finding, not a footnote.** If the data is beyond the contract's
  `max_lag`, lead with that.
