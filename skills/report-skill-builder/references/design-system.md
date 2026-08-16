# Report design system

The bundled look for generated reports: a shadcn/ui dashboard — stat cards, chart cards, a table —
drawn with the same tokens and the same chart library the Databricks App consumer uses.

That symmetry is the point. `assets/report-charts.js` builds Apache ECharts option objects — the
same library `@databricks/appkit-ui` depends on. Hand the identical `option` to
`<ReactECharts option={...} />` inside AppKit and you get the same chart. One definition, two
consumers.

ECharts also needs no framework: one script, where a React-based chart library needs four
(React + ReactDOM + prop-types + the library, ~657 KB against ECharts' 493 KB `simple` build).

**The trade this makes.** The chart runtime comes from a CDN, pinned exactly, so the plots need
the network. Figures, tables, states and provenance are in the file and render
without it, and every chart slot carries a visible fallback rather than going silently blank. If a
report must draw offline, that is an argument for inlining the runtime, not for hoping.

**Contents:** [Rules that matter](#rules-that-matter) · [Tokens](#tokens) ·
[Typography](#typography) · [Copy](#copy) · [AI summaries](#ai-summaries) · [Charts](#charts) ·
[States](#states) · [Constraints](#constraints) · [Extending it](#extending-it) ·
[Accessibility](#accessibility)

## Rules that matter

The default failure is a wall of grey prose with a table at the bottom. Four rules prevent it:

1. **Every block leads with a number, not a sentence.**
2. **No paragraph. Two sentences maximum, anywhere.**
3. **Colour does two jobs.** Blue is the business, red is money leaving. Nothing else gets a hue,
   and direction never depends on one.
4. **Everything except the plots renders without the network**, and the plots say so when they
   cannot draw.

## Tokens

All of it lives in `assets/report.css` as CSS custom properties. Change values there; do not add
hex codes in markup.

| Token | Role |
| --- | --- |
| `--background` / `--foreground` | page and body text |
| `--card` / `--border` | card surface and its hairline |
| `--muted-foreground` | labels, axis ticks, table headers |
| `--chart-1..3` | blue: result, subtotal, opening stock |
| `--destructive` | money leaving, negative figures |
| `--radius` | 0.625rem, shadcn's default |

Values are oklch, as shadcn ships them, and referenced as `var(--chart-1)` — not
`hsl(var(--chart-1))`.

A dark palette is supplied via `prefers-color-scheme`. Note that oklch is still weaker in some
PDF pipelines than hsl — if print fidelity matters more than matching shadcn exactly, that is the
token to reconsider first.

## Typography

System font stack only — no webfont, so type renders the same with or without the network.

| Element | Size | Notes |
| --- | --- | --- |
| body | 14px | sentence case throughout; tracked uppercase kickers are the dated tell |
| stat value | clamp(21px, 1.7vw, 27px), weight 600 | `tabular-nums`, −0.02em tracking |
| card title | 15px, weight 600 | one per card, sentence case |
| chart tick | 15px | above the legibility floor; Recharts defaults lower |
| table | 14px | numeric cells right-aligned and tabular |

Numbers are always `font-variant-numeric: tabular-nums`. Proportional digits make two figures of
the same magnitude look different lengths, which is exactly the comparison a report exists to make.

## Copy

| Element | Budget |
| --- | --- |
| KPI label | ≤ 3 words, no units in the label (put them in the value) |
| Block title | ≤ 4 words |
| AI summary | ≤ 2 sentences |
| Table caption | one noun phrase |

If a sentence explains what the number already says, delete the sentence.

## AI summaries

The summary is the only text a model writes, and the part a reader is most likely to quote, so it
carries the strictest rules:

- **Set as a note, not a widget.** It sits at the foot of its own card — no separate panel, no
  left border, no pill chips. It should read as that block's takeaway.
- **Still attributed.** One small `AI` mark precedes it, and screen readers get the full
  "AI-generated summary" prefix. A reader must always be able to tell model prose from contract
  output; that does not require a badge.
- **Grounded.** Every number in the sentence must appear in that block's rows. A summary that
  computes its own percentage is a fabrication in a trustworthy font.
- **Additive.** Say the direction, the driver, or the threshold crossed — not the number again.
  Better still, state the claim the chart then proves.
- **Escaped.** It goes through the same escaping as every value; a summary is untrusted text.

Good: *"Margin held inside a 4.8 point band all year, bottoming in January before seven straight
months of recovery."*
Bad: *"This report shows the P&L for the period, including revenue, costs and margin."*

That "seven" is the whole rule in miniature. An earlier draft of this very example said *nine* —
a number nobody had counted, sitting in a sentence that otherwise read perfectly. Count the run
against the rows before you write it down; a summary is the easiest place in a governed report to
introduce a figure that no query produced.

## Charts

ECharts, styled to shadcn's chart conventions: horizontal-only dashed `splitLine`, `axisLine` and
`axisTick` hidden, bar `borderRadius: [4,4,0,0]`, SVG renderer so charts print and scale cleanly.

- **Mark by shape of data.** Seven or more rows renders as an area (a series); fewer renders as
  bars (categories). The renderer picks; the contract does not need a chart type.
- **`animation: false` everywhere.** An animated series is mid-flight when a screenshot, print or
  PDF is taken, so a static capture of an animated chart is blank or half-drawn. Not a preference.
- **Axis labels at 15px**, not the ~12px chart libraries default to, which is under the floor.
- **A padded axis is labelled as one.** A narrow band on an auto axis reads flat, so the domain is
  padded and rounded to a nice step — and the caption says "not zero-based" out loud. A silent
  axis break is the thing to avoid, not a stated one.
- Direction is never colour alone: negative figures carry a sign and a red token together.

## States

A block never disappears. Silence reads as "zero" and that is the most expensive bug a report has.

| State | Rendering |
| --- | --- |
| `ok` | normal |
| `empty` | title plus "No rows for the selected parameters" in muted italic |
| `partial` | normal, preceded by a flagged line naming the truncation or cap |
| `error` | flagged panel with the word "Failed" and the error string, still in document order |

An unrecognised status is treated as an error. Rendering an unknown state as data is how a broken
block gets read as a real number.

Provenance — contract version, owner, warehouse, attested principal, watermark and each block's
trust class and status — is written into the page as an HTML comment rather than a footer table.
The audit trail travels with the artifact; it just does not take up the reader's attention.

## Constraints

- One HTML file. Styles inline, no `@import`, no webfont.
- A `Content-Security-Policy` meta tag allows scripts only from the pinned CDN origin and blocks
  everything else, including `connect-src` — the chart runtime may draw, it may not phone home.
- Library versions are pinned exactly. A floating version would change the report under you.
- `@media print` sets landscape, forces black-on-white, and avoids breaking a block across pages.
- The HTML is a data extract. Treat it like the rows it contains: do not commit it, and mind where
  it gets emailed.

## Extending it

Add a token or a component class in `report.css` and a small renderer function in
`render_report.py`, or a chart type in `report-charts.js`. Do not fork the template per report —
every generated skill carries the same asset files, and a per-report fork is how a design system
dies. A new chart type added here is one you can lift straight into the AppKit app. For a deployment that needs a
specific layout (fitting one fixed screen, say), layer it on with `render_report.py --extra-css`
rather than editing the shipped stylesheet.

## Accessibility

- Direction is never colour-only: the deviation carries a sign, the flag carries a word.
- Every chart has `role="img"` and an `aria-label` naming the block.
- Body contrast is 4.5:1 or better in both palettes; muted text is reserved for labels, never data.
