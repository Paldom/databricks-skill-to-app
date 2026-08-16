# Changelog

All notable changes to this repository's skills are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org) on the plugin manifest
(breaking skill-interface change → major, new skill → minor, fix → patch).

## [Unreleased]

### Added
- `governed-report-contract` — authors and validates a versioned report contract (`report.yaml`,
  parametric `queries/*.sql`, metric-view bindings) with a deterministic validator
  (`validate_contract.py`, 30-case self-test) covering parameter parity, total ordering, catalog
  allowlisting, read-only enforcement and identity/cache safety.
- `report-skill-builder` — generates a self-contained report skill from a contract: materialized
  contract copy plus SHA-256 manifest, a Statement Execution runner (polling, chunk pagination,
  principal attestation, parameter bounds), a single-file HTML renderer with a shadcn-style design
  system, and generated evals.
- `report-to-databricks-app` — materializes a contract into an AppKit app (`config/queries/`,
  `config/metric-views/definitions.json`) preserving `.obo.sql` execution identity, with a
  `--check` drift gate for CI.
- `docs/setup-prompt.md` — paste-ready `/goal` running contract → skill → app end to end.
- `examples/monthly-pnl/` — worked example built and run against a real workspace: synthetic P&L
  fact table, a Unity Catalog metric view owning the ratios, the contract, the generated report
  skill, the materialized app config, and a DAB (`bundle validate --strict` passes) that recreates
  it. Includes the real rendered report and result envelope.
- `docs/assets/report-sample.png`, `app-sample.png` and `omnigent-skill-run.png` — 1920x1080
  captures of the rendered report, the running AppKit app, and an agent invoking the generated
  skill end to end, all from a live workspace.

### Changed
- Report engine rebuilt on shadcn/ui tokens and Apache ECharts — the library `@databricks/appkit-ui`
  itself depends on. `assets/report-charts.js` builds plain ECharts `option` objects, so the same
  object renders via `<ReactECharts>` in an AppKit app. ECharts needs no framework, cutting the
  runtime from four scripts (~657 KB) to one (493 KB). Blocks now render as shadcn
  stat cards, chart cards and tables. Chart type follows the data (>= 7 rows area, else bars), the
  axis domain is padded, rounded and labelled "not zero-based" when it is, and every series sets
  `isAnimationActive: false` so static captures are not blank.
- Chart data is embedded as JSON in a `<script>` block, which is a second injection surface: it is
  escaped separately (`<`, `>`, `&` as `\u` escapes) and the self-test asserts nothing can break
  out of the tag.
- The report now needs the network for plots only. Figures, tables, states and provenance render
  without it, and each chart slot carries a visible fallback rather than a blank box.
- Report design reworked into an editorial sheet: cards, chips, badges, small-caps kickers and the
  provenance footer are gone; structure now comes from type and hairlines. Provenance travels with
  the file as an HTML comment instead of a footer table.
- Narrow-range series render as deviation lollipops against the period average — the baseline is
  printed, every point keeps its actual value and a signed difference — instead of zero-based bars
  where every bar looks the same length.
- Period and freshness render as a segmented header rail (one segment per month, covered run
  filled) rather than a row of `key: value` text.
- AI summaries are set as a serif-italic lede with one small `AI` mark, not a bordered card with
  pill chips.
- `render_report.py` gained `--extra-css` so a deployment can layer layout tweaks without forking
  the shipped stylesheet; the watermark block is no longer rendered twice.
- README leads with a 1920x1080 screenshot of the rendered report, plus one of the AppKit app
  showing the same figures. The terminal recording and the composed walkthrough video were dropped
  as noise.
- The app half was verified end to end against a real workspace: `databricks apps init`,
  `materialize_app.py` (which stripped the scaffold's template queries as ungoverned), typegen
  describing all four contract queries against the warehouse, and the app serving the same figures
  the report shows.

### Fixed
- Chart labels were rendering at roughly 12.7px instead of the declared 11px-and-up because the
  SVG carried a `viewBox` but no fixed width, so its text scaled with the geometry. The chart is
  now authored 1:1 (`width`/`height` in real px) and its type raised to 15-16px, which puts it
  above the desk-viewing legibility floor. This is the usual reason charts in generated HTML
  reports come out unreadable.
- Documented the correct Statement Execution path `POST /api/2.0/sql/statements/`; the `/execute`
  form used by the official `databricks-metric-views` skill returns `No API found` (verified
  against a live workspace).
- Recorded that `databricks-apps`' SQL reference is stale in listing `sql.int()` / `sql.float()` as
  non-existent — both are documented in the shipped AppKit docs.

### Removed
- Repository scaffolding placeholder from the skills catalog.
