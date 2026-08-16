#!/usr/bin/env python3
"""Render a result envelope into an HTML report — shadcn tokens, Recharts charts.

Every value that reaches the page is escaped: a report inlines database content and model-written
prose, so the page is a trust boundary. Chart data is embedded as JSON inside a <script> tag,
which is a second injection surface with its own escaping rule.

Charts are drawn by Recharts from a CDN. Figures, tables, states and provenance are in the file
and render without it; only the plots need the network. The chart component code is deliberately
the shape AppKit uses, so it ports by deleting the CDN tags.

Usage:
    python3 render_report.py --envelope report.json --out report.html \
        --summary pnl_summary="Net result held at 19% margin; EMEA carried the quarter."

Exit codes: 0 = rendered, 1 = rendered with failed blocks, 2 = cannot run.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

NUMERIC_TYPES = {"INT", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "LONG"}
AREA_MIN_ROWS = 7          # more points than this reads as a series, fewer as categories


def esc(value: object) -> str:
    """The only way text reaches the page."""
    return html.escape("" if value is None else str(value), quote=True)


def json_for_script(payload: object) -> str:
    """Serialize for embedding in <script type="application/json">.

    A `</script>` inside any string value would close the tag and turn the rest of the data into
    markup, so every character that can start a tag is escaped. json.loads reads them back
    unchanged, because \\u escapes are just characters to a JSON parser.
    """
    raw = json.dumps(payload, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def is_numeric(col: dict) -> bool:
    return str(col.get("type") or "").upper().split("(")[0] in NUMERIC_TYPES


def label(name: object) -> str:
    """SQL aliases are snake_case; a report is read by people. `net_result` -> `Net result`."""
    text = str(name or "").replace("_", " ").strip()
    if text.endswith(" pct"):
        text = text[:-4] + " %"
    return (text[:1].upper() + text[1:]) if text else ""


def fmt(value: object, numeric: bool) -> str:
    if value is None:
        return "—"
    if not numeric:
        return esc(value)
    try:
        # DECIMAL and large BIGINT arrive as strings. Group the integer digits and keep the
        # fraction EXACTLY as returned — rounding is the query's job.
        text = str(value)
        neg = text.startswith("-")
        whole, dot, frac = text.lstrip("-").partition(".")
        if not whole.isdigit():
            return esc(value)
        return esc(("-" if neg else "") + f"{int(whole):,}" + (f".{frac}" if dot else ""))
    except (ValueError, TypeError):
        return esc(value)


def to_number(value: object):
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- freshness

def parse_date(value: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def month_label(ts: datetime | None) -> str:
    return ts.strftime("%b %Y") if ts else ""


def freshness_state(freshness: dict) -> str:
    watermark, max_lag = freshness.get("watermark"), freshness.get("max_lag")
    if not watermark or not max_lag:
        return ""
    m = re.fullmatch(r"(\d+)\s*([hdm])", str(max_lag).strip(), re.IGNORECASE)
    if not m:
        return ""
    seconds = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
    ts = parse_date(watermark)
    if ts is None:
        return ""
    return "stale" if (datetime.now(timezone.utc) - ts).total_seconds() > seconds else "fresh"


def timeframe_html(params: dict, freshness: dict) -> str:
    """Period and freshness as a spine: the filled run is the data you actually have."""
    start, end = parse_date(params.get("start_date")), parse_date(params.get("end_date"))
    watermark = parse_date(freshness.get("watermark"))
    state = freshness_state(freshness)
    if not (start and end) or end <= start:
        if not watermark:
            return ""
        return (f'<div class="timeframe"><p class="tf-note">complete through '
                f'{esc(month_label(watermark))}</p></div>')

    covered = 1.0
    if watermark:
        covered = max(0.0, min(1.0, (watermark - start).total_seconds() / (end - start).total_seconds()))
    months = max(1, min(24, round((end - start).days / 30.44) + 1))
    filled = max(1, round(covered * months)) if watermark else 0
    segs = "".join(
        f'<span class="tf-seg {"now" if i == filled - 1 else "on" if i < filled else ""}"></span>'
        for i in range(months))
    note = (f"complete through {esc(month_label(watermark))}" if covered >= 0.999
            else f"data ends {esc(month_label(watermark))}") if watermark else "no watermark"
    if state == "stale":
        note += " · stale"
    return (
        '<div class="timeframe">'
        f'<p class="tf-dates">{esc(month_label(start))} — {esc(month_label(end))}</p>'
        f'<span class="tf-spine" role="img" aria-label="Data covers {filled} of {months} months, '
        f'{esc(note)}">{segs}</span>'
        f'<p class="tf-note{" stale" if state == "stale" else ""}">{note}</p>'
        "</div>"
    )


# --------------------------------------------------------------------------- blocks

def card(body: str, extra: str = "") -> str:
    cls = f"card {extra}".strip()
    return f'<div class="{cls}">{body}</div>'


def summary_html(block: dict) -> str:
    """The generated sentence reads as the block's note, attributed without a chip."""
    s = block.get("summary")
    text = (s.get("text") if isinstance(s, dict) else s) if s else None
    if not text:
        return ""
    return (f'<p class="insight"><span class="insight-mark" aria-hidden="true">AI</span>'
            f'<span class="sr-only">AI-generated summary: </span>{esc(text)}</p>')


def partial_alert(block: dict) -> str:
    if block.get("status") == "partial":
        note = block.get("error") or "result truncated by the server"
        return f'<p class="alert"><span class="flag">Partial</span>{esc(note)}</p>'
    return ""


def state_card(block: dict) -> str:
    """A failed block stays on the page. Silence reads as zero, the costliest bug a report has."""
    head = f'<p class="card-title">{esc(block.get("title"))}</p>'
    if block.get("status") == "error":
        return card(f'{head}<p class="empty"><span class="flag">Failed</span>'
                    f"this block did not return data.</p>"
                    f'<code>{esc(block.get("error"))}</code>', "block-error")
    return card(f'{head}<p class="empty">No rows for the selected parameters.</p>')


def kpi_cards(block: dict) -> str:
    cols, rows = block.get("columns") or [], block.get("rows") or []
    if not rows:
        return state_card({**block, "status": "empty"})
    out = []
    for col, value in zip(cols, rows[0]):
        numeric = is_numeric(col)
        n = to_number(value) if numeric else None
        badge = '<span class="badge down">▼ negative</span>' if n is not None and n < 0 else ""
        out.append(card(f'<p class="card-desc">{esc(label(col.get("name")))}</p>'
                        f'<p class="stat">{fmt(value, numeric)}</p>{badge}'))
    return "".join(out)


def table_card(block: dict) -> str:
    cols, rows = block.get("columns") or [], block.get("rows") or []
    numeric = [is_numeric(c) for c in cols]
    head = "".join(f"<th>{esc(label(c.get('name')))}</th>" for c in cols)
    body = ""
    for r in rows:
        cells = []
        for value, num in zip(r, numeric):
            n = to_number(value) if num else None
            cls = ' class="neg"' if n is not None and n < 0 else ""
            cells.append(f"<td{cls}>{fmt(value, num)}</td>")
        body += "<tr>" + "".join(cells) + "</tr>"
    return card(f'<p class="card-title">{esc(block.get("title"))}</p>{partial_alert(block)}'
                f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{body}</tbody></table></div>{summary_html(block)}")


def chart_card(block: dict, specs: dict) -> str:
    """Register a Recharts spec and leave a mount point with an honest fallback."""
    cols, rows = block.get("columns") or [], block.get("rows") or []
    if len(cols) < 2 or not rows:
        return table_card(block)

    x_key = str(cols[0].get("name"))
    series = [{"key": str(c.get("name")), "label": label(c.get("name"))}
              for c in cols[1:] if is_numeric(c)]
    if not series:
        return table_card(block)

    data, values = [], []
    for r in rows:
        point = {x_key: str(r[0])}
        for col, value in zip(cols[1:], r[1:]):
            n = to_number(value)
            if n is not None:
                point[str(col.get("name"))] = n
                values.append(n)
        data.append(point)
    if not values:
        return table_card(block)

    # A narrow band on an auto axis reads flat. Pad the domain rather than forcing zero, and say
    # so under the chart instead of letting the axis imply a zero baseline it does not have.
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.15, abs(hi) * 0.02, 0.5)
    # Round the bounds to a nice step, or the axis prints ticks like 47.634.
    step = 10 ** math.floor(math.log10(max(hi - lo, abs(hi), 1e-9)))
    zero_based = 0 <= lo <= (hi - lo)
    domain = ([0, math.ceil((hi + pad) / step) * step] if zero_based
              else [math.floor((lo - pad) / step) * step, math.ceil((hi + pad) / step) * step])
    domain = [round(d, 6) for d in domain]

    chart_id = "chart-" + re.sub(r"[^a-z0-9_-]", "", str(block.get("key")).lower())
    specs[chart_id] = {
        "type": "area" if len(rows) >= AREA_MIN_ROWS else "bar",
        "xKey": x_key, "series": series, "data": data, "domain": domain,
    }
    axis_note = ("" if zero_based else
                 f'<p class="note">Axis spans {domain[0]:g} to {domain[1]:g}, not zero-based.</p>')
    return card(f'<p class="card-title">{esc(block.get("title"))}</p>{partial_alert(block)}'
                f'<div class="chart-box"><div id="{chart_id}" style="height:100%">'
                f'<p class="chart-fallback">Chart runtime unavailable — this file needs a network '
                f"connection to draw plots. The figures are in the table blocks.</p></div></div>"
                f"{axis_note}{summary_html(block)}")


def narrative_card(block: dict) -> str:
    rows = block.get("rows") or []
    text = rows[0][0] if rows and rows[0] else ""
    return card(f'<p class="card-title">{esc(block.get("title"))}</p>'
                f"<p>{esc(text)}</p>{summary_html(block)}")


# --------------------------------------------------------------------------- page

def render(envelope: dict, style: str, template: str, bootstrap: str) -> tuple[str, int]:
    contract = envelope.get("contract") or {}
    freshness = envelope.get("freshness") or {}
    watermark_key = freshness.get("source_block")

    specs: dict = {}
    stats, panels, failed = [], [], 0

    for block in envelope.get("blocks") or []:
        status = block.get("status")
        # An unrecognised status is a failure, not a success.
        if status not in ("ok", "partial", "empty", "error"):
            block = {**block, "status": "error",
                     "error": f"unknown block status {status!r} — refusing to render it as data"}
            status = "error"
        # The watermark block feeds the header spine; rendering it again as a lone date is noise.
        if block.get("key") == watermark_key and status in ("ok", "empty"):
            continue
        if status in ("error", "empty"):
            if status == "error":
                failed += 1
            panels.append(state_card(block))
            continue

        kind = block.get("kind") or "table"
        if kind == "kpi":
            stats.append(kpi_cards(block))
        elif kind == "chart":
            panels.append(chart_card(block, specs))
        elif kind == "narrative":
            panels.append(narrative_card(block))
        else:
            panels.append(table_card(block))

    body = ""
    if stats:
        body += f'<div class="grid-stats">{"".join(stats)}</div>'
    if panels:
        body += f'<div class="grid-main">{"".join(panels)}</div>'

    params = envelope.get("params") or {}
    subtitle = esc(" · ".join(f"{k} {v}" for k, v in params.items())) or esc(contract.get("owner") or "")
    meta = timeframe_html(params, freshness)
    if freshness_state(freshness) == "stale":
        meta += (f'<p class="alert">Data is older than the contract\'s limit of '
                 f'{esc(freshness.get("max_lag"))}.</p>')

    audit = {
        "contract": f'{contract.get("name")} v{contract.get("version")}',
        "owner": contract.get("owner"),
        "generated_at": envelope.get("generated_at"),
        "warehouse": envelope.get("warehouse_id"),
        "executed_as": envelope.get("attested_principal") or "unverified",
        "watermark": freshness.get("watermark"),
        "blocks": {b.get("key"): f'{b.get("trust")}/{b.get("identity")}/{b.get("status")}'
                   for b in envelope.get("blocks") or []},
    }
    trail = "<!-- provenance " + json.dumps(audit).replace("--", "- -") + " -->"

    page = (template
            .replace("{{STYLE}}", style)
            .replace("{{TITLE}}", esc(contract.get("title") or "Report"))
            .replace("{{SUBTITLE}}", subtitle)
            .replace("{{META}}", meta)
            .replace("{{BLOCKS}}", body)
            .replace("{{CHART_DATA}}", json_for_script(specs))
            .replace("{{BOOTSTRAP}}", bootstrap)
            .replace("{{PROVENANCE}}", trail))
    return page, failed


# --------------------------------------------------------------------------- self-test

def selftest() -> int:
    """Hostile values must never escape into markup — or out of the JSON script block."""
    payload = '</script><img src=x onerror=alert(1)>'
    envelope = {
        "contract": {"name": "t", "version": "1.0.0", "title": payload, "owner": "o"},
        "generated_at": "2026-01-01T00:00:00Z",
        "params": {"entity": payload},
        "attested_principal": "someone@example.com",
        "freshness": {"watermark": "2020-01-01T00:00:00Z", "max_lag": "26h"},
        "blocks": [
            {"key": "k", "kind": "kpi", "title": payload, "identity": "user", "trust": "certified",
             "status": "ok", "columns": [{"name": payload, "type": "DECIMAL(18,2)"},
                                         {"name": "loss", "type": "DECIMAL(18,2)"}],
             "rows": [["12345.678", "-42"]], "summary": {"text": payload}},
            {"key": "t2", "kind": "table", "title": "T", "identity": "service_principal",
             "trust": "certified", "status": "ok",
             "columns": [{"name": "a", "type": "STRING"}], "rows": [[payload]]},
            {"key": "c", "kind": "chart", "title": payload, "identity": "service_principal",
             "trust": "certified", "status": "ok",
             "columns": [{"name": payload, "type": "STRING"}, {"name": "v", "type": "DOUBLE"}],
             "rows": [[payload, str(41 + i * 0.5)] for i in range(12)]},
            {"key": "big", "kind": "table", "title": "Big", "identity": "user", "trust": "certified",
             "status": "ok", "columns": [{"name": "n", "type": "BIGINT"}],
             "rows": [["9007199254740993"]]},
            {"key": "e", "kind": "table", "title": "E", "status": "error", "error": payload},
            {"key": "weird", "kind": "kpi", "title": "W", "status": "totally-fine",
             "columns": [{"name": "x", "type": "INT"}], "rows": [["1"]]},
        ],
    }
    here = Path(__file__).resolve().parent
    page, failed = render(envelope,
                          (here / "report.css").read_text(encoding="utf-8"),
                          (here / "report-template.html").read_text(encoding="utf-8"),
                          (here / "report-charts.js").read_text(encoding="utf-8"))

    problems = []
    if "<img" in page:
        problems.append("unescaped HTML payload reached the page as live markup")
    if "&lt;img src=x" not in page:
        problems.append("payload was dropped instead of escaped — data must survive, escaped")

    # The JSON block is a second injection surface with its own rule.
    data = page.split('id="chart-data" type="application/json">')[1].split("</script>")[0]
    if "<" in data or ">" in data:
        problems.append("raw angle bracket inside the JSON script block — can break out of the tag")
    try:
        specs = json.loads(data)
    except ValueError as exc:
        specs = {}
        problems.append(f"embedded chart JSON does not parse: {exc}")
    if not specs:
        problems.append("chart spec did not survive serialization")
    elif payload not in json.dumps(specs):
        problems.append("chart data lost its values in escaping")

    if "12,345.678" not in page:
        problems.append("DECIMAL precision was altered")
    if "9,007,199,254,740,993" not in page:
        problems.append("a BIGINT above 2^53 lost precision")
    if failed != 2:
        problems.append(f"expected 2 failed blocks, counted {failed}")
    if "insight-mark" not in page:
        problems.append("model-written prose lost its attribution mark")
    if "<!-- provenance" not in page or '"executed_as"' not in page:
        problems.append("the provenance trail is missing from the artifact")
    if "chart-fallback" not in page:
        problems.append("chart slot has no offline fallback message")
    if "animation: false" not in page and "animation:false" not in page:
        problems.append("chart runtime missing or animating — a static capture would show empty plots")
    if "stale" not in page:
        problems.append("a watermark beyond max_lag was not reported as stale")

    if problems:
        for p in problems:
            print(f"SELFTEST FAIL: {p}", file=sys.stderr)
        return 1
    print("OK: self-test passed (html escaping, json-in-script escaping, numeric fidelity, "
          "failure states, chart spec, provenance)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--envelope", type=Path, help="result envelope from run_report.py")
    ap.add_argument("--out", type=Path, help="HTML file to write")
    ap.add_argument("--summary", action="append", default=[], metavar="BLOCK=TEXT",
                    help="attach a short AI summary to a block")
    ap.add_argument("--extra-css", type=Path,
                    help="append a stylesheet after the design system (per-deployment layout)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.envelope or not args.out:
        ap.error("--envelope and --out are required (or pass --selftest)")
    if not args.envelope.is_file():
        print(f"ERROR: {args.envelope} not found", file=sys.stderr)
        return 2

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    for item in args.summary:
        if "=" not in item:
            print(f"ERROR: --summary expects BLOCK=TEXT, got {item!r}", file=sys.stderr)
            return 2
        key, text = item.split("=", 1)
        for block in envelope.get("blocks") or []:
            if block.get("key") == key:
                block["summary"] = {"text": text}
                break
        else:
            print(f"ERROR: no block named {key!r} in the envelope", file=sys.stderr)
            return 2

    here = Path(__file__).resolve().parent
    assets = here if (here / "report.css").is_file() else here.parent / "assets"
    style = (assets / "report.css").read_text(encoding="utf-8")
    if args.extra_css:
        if not args.extra_css.is_file():
            print(f"ERROR: {args.extra_css} not found", file=sys.stderr)
            return 2
        style += "\n\n/* --- extra-css --- */\n" + args.extra_css.read_text(encoding="utf-8")
    page, failed = render(envelope, style,
                          (assets / "report-template.html").read_text(encoding="utf-8"),
                          (assets / "report-charts.js").read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"{'FAIL' if failed else 'OK'}: rendered {args.out} ({failed} failed block(s))")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
