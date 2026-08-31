#!/usr/bin/env python3
"""Generate a self-contained report skill from a validated governed report contract.

The generated skill embeds a materialized copy of the contract plus a SHA-256 manifest, so it
runs the same SQL as every other consumer and refuses to run once that copy drifts.

Usage:
    python3 new_report_skill.py --contract reports/monthly-pnl --out .claude/skills/monthly-pnl
    python3 new_report_skill.py --selftest

Exit codes: 0 = generated, 1 = contract invalid, 2 = cannot run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
COPY_TO_SCRIPTS = ("run_report.py", "render_report.py")
COPY_TO_ASSETS = ("report.css", "report-template.html", "report-charts.js")
DESC_MIN, DESC_MAX = 150, 400


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def find_validator(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit if explicit.is_file() else die(f"--validator {explicit} not found")
    sibling = ASSETS.parent.parent / "governed-report-contract" / "scripts" / "validate_contract.py"
    return sibling if sibling.is_file() else None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_description(title: str, name: str, version: str) -> str:
    desc = (
        f"Generates the {title} report as a self-contained HTML page from the pinned {name} "
        f"contract v{version}, with the numbers traceable to its trusted queries. Use when the "
        f"user asks for the {title}, this period's figures, or to re-run or refresh that report. "
        f"Not for changing the report definition, other reports, dashboards or app deployment."
    )
    if len(desc) > DESC_MAX:
        desc = (
            f"Generates the {title} report as a self-contained HTML page from the pinned {name} "
            f"contract v{version}. Use when the user asks for the {title}, this period's figures, "
            f"or to re-run that report. Not for changing the report definition, other reports, "
            f"dashboards or app deployment."
        )
    return desc


def render_evals(skill_name: str, title: str) -> dict:
    """A generated skill still needs its own routing evals, or nothing will route to it."""
    lower = title.lower()
    return {
        "skill": skill_name,
        "cases": [
            {"type": "should_trigger", "prompt": f"generate the {lower}"},
            {"type": "should_trigger", "prompt": f"run the {lower} for last month"},
            {"type": "should_trigger", "prompt": f"can I get the {lower} as html?"},
            {"type": "should_trigger", "prompt": f"refresh the {lower}, the numbers look stale"},
            {"type": "should_trigger", "prompt": f"i need this period's {lower} figures"},
            {"type": "should_trigger", "prompt": f"{lower} pls"},
            {"type": "should_trigger", "prompt": f"re-run the {lower} with a wider date range"},
            {"type": "should_trigger", "prompt": f"send me the latest {lower} report"},
            {"type": "should_not_trigger", "prompt": f"change how the {lower} calculates margin"},
            {"type": "should_not_trigger", "prompt": f"add a new metric to the {lower}"},
            {"type": "should_not_trigger", "prompt": f"turn the {lower} into a databricks app"},
            {"type": "should_not_trigger", "prompt": f"build a dashboard for the {lower}"},
            {"type": "should_not_trigger", "prompt": "what tables are in the finance schema?"},
            {"type": "should_not_trigger", "prompt": "write a SQL query for revenue by region"},
            {"type": "should_not_trigger", "prompt": "validate our report contract"},
            {"type": "should_not_trigger", "prompt": "create a metric view"},
            {
                "type": "quality",
                "prompt": f"generate the {lower}",
                "expected_behavior": [
                    "runs the bundled runner against the materialized contract instead of writing new SQL",
                    "writes one self-contained HTML file and gives the user its path",
                    "states the contract version, the data watermark and the attested principal",
                    "flags any block that failed or came back partial instead of omitting it",
                ],
            },
            {
                "type": "quality",
                "prompt": f"summarise what changed in the {lower}",
                "expected_behavior": [
                    "keeps each summary to at most two sentences",
                    "only cites numbers that appear in the block's rows",
                    "labels the text as AI-generated with its provenance",
                ],
            },
            {
                "type": "quality",
                "prompt": f"the {lower} data looks old, is it?",
                "expected_behavior": [
                    "reads the freshness watermark from the envelope rather than the generation timestamp",
                    "compares it against the contract's max_lag and says plainly whether it is stale",
                ],
            },
        ],
    }


def safe_token(value: str, field: str) -> str:
    """Values are substituted into YAML frontmatter, Markdown tables and shell commands.

    A newline breaks the frontmatter, a `{{` re-enters the templating pass, and a backtick or
    `$(` in a default value becomes a command substitution in a copied command line.
    """
    text = " ".join(str(value).split())
    if "{{" in text or "}}" in text:
        die(f"{field} contains template tokens ({{{{ }}}}) — rename it in the contract")
    for bad in ("`", "$(", "\\"):
        if bad in text:
            die(
                f"{field} contains {bad!r}, which would execute or escape in a generated command "
                "— rename it in the contract"
            )
    return text


def yaml_quote(text: str) -> str:
    """Emit a double-quoted YAML scalar.

    A collapsed title can still contain `: `, which an unquoted scalar reads as a nested mapping —
    the skill then silently fails to load. Quoting removes the whole class of problem.
    """
    return '"' + text.replace('"', '\\"') + '"'


def generate(
    contract: Path, out: Path, validator: Path | None, skip_validate: bool, force: bool = False
) -> int:
    try:
        import yaml
    except ImportError:
        die("PyYAML is required — `pip install pyyaml`")

    if not (contract / "report.yaml").is_file():
        die(f"{contract}/report.yaml not found — generate from a contract directory")

    # Generating into (or from) an overlapping path would delete the source before copying it.
    if contract == out or contract.is_relative_to(out) or out.is_relative_to(contract):
        die(
            f"--contract {contract} and --out {out} overlap — the contract would be deleted; "
            "generate into a separate directory"
        )
    if out.exists() and any(out.iterdir()) and not force:
        die(f"{out} already exists and is not empty — pass --force to replace it")

    if not skip_validate:
        if validator is None:
            die(
                "cannot find validate_contract.py — pass --validator <path>, or --skip-validate "
                "only if you have already run it and it exited 0"
            )
        proc = subprocess.run(
            [sys.executable, str(validator), str(contract)], capture_output=True, text=True
        )
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            die("the contract is invalid — fix it before generating a skill from it", code=1)

    spec = yaml.safe_load((contract / "report.yaml").read_text(encoding="utf-8")) or {}
    name = safe_token(spec.get("name") or contract.name, "report.yaml name")
    title = safe_token(spec.get("title") or name, "report.yaml title")
    version = safe_token(spec.get("version") or "0.0.0", "report.yaml version")
    owner = safe_token(spec.get("owner") or "unknown", "report.yaml owner")
    skill_name = out.name

    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", skill_name):
        die(f"the output folder name {skill_name!r} must be kebab-case — it becomes the skill name")

    # --- materialize the contract -------------------------------------------------------
    dest_contract = out / "contract"
    if dest_contract.exists():
        shutil.rmtree(dest_contract)
    dest_contract.mkdir(parents=True)
    shutil.copy2(contract / "report.yaml", dest_contract / "report.yaml")
    for sub in ("queries", "metric-views"):
        if (contract / sub).is_dir():
            shutil.copytree(contract / sub, dest_contract / sub)

    files = {
        str(p.relative_to(dest_contract)): sha256(p)
        for p in sorted(dest_contract.rglob("*"))
        if p.is_file()
    }
    (out / "contract.manifest.json").write_text(
        json.dumps(
            {"contract": name, "version": version, "source": contract.name, "files": files},
            indent=2,
        ),
        encoding="utf-8",
    )

    # --- copy the runner, renderer and design system -------------------------------------
    (out / "scripts").mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    for f in COPY_TO_SCRIPTS:
        shutil.copy2(ASSETS / f, out / "scripts" / f)
    for f in COPY_TO_ASSETS:
        shutil.copy2(ASSETS / f, out / "assets" / f)

    # --- render SKILL.md ------------------------------------------------------------------
    blocks = spec.get("blocks") or []
    blocks_table = (
        "\n".join(
            f"| `{b.get('key')}` | {b.get('kind')} | {b.get('identity')} | {b.get('trust')} |"
            for b in blocks
        )
        or "| _none_ | | | |"
    )
    params = [
        {k: safe_token(v, f"params.{k}") for k, v in p.items() if k in ("name", "type", "default")}
        for p in (spec.get("params") or [])
    ]
    params_table = (
        "\n".join(
            f"| `{p.get('name')}` | {p.get('type')} | `{p.get('default', '')}` |" for p in params
        )
        or "| _none_ | | |"
    )
    param_flags = "".join(
        f"     --param {p.get('name')}={p.get('default', '<value>')} \\\n" for p in params
    )

    description = build_description(title, name, version)
    if not DESC_MIN <= len(description) <= DESC_MAX:
        print(
            f"WARN: generated description is {len(description)} chars (aim {DESC_MIN}-{DESC_MAX}) "
            f"— edit it in {out / 'SKILL.md'}",
            file=sys.stderr,
        )

    skill_md = (ASSETS / "SKILL.md.tmpl").read_text(encoding="utf-8")
    for token, value in (
        ("{{SKILL_NAME}}", skill_name),
        ("{{DESCRIPTION}}", yaml_quote(description)),
        ("{{REPORT_TITLE}}", title),
        ("{{REPORT_NAME}}", name),
        ("{{VERSION}}", version),
        ("{{OWNER}}", owner),
        ("{{BLOCKS_TABLE}}", blocks_table),
        ("{{PARAMS_TABLE}}", params_table),
        ("{{PARAM_FLAGS}}", param_flags),
    ):
        skill_md = skill_md.replace(token, value)
    (out / "SKILL.md").write_text(skill_md, encoding="utf-8")

    (out / "evals").mkdir(parents=True, exist_ok=True)
    (out / "evals" / "evals.json").write_text(
        json.dumps(render_evals(skill_name, title), indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"OK: generated {out} from {name} v{version} ({len(blocks)} block(s), {len(files)} contract file(s))"
    )
    return 0


def selftest() -> int:
    import tempfile

    contract_yaml = """\
version: 2.1.0
name: demo
title: Demo Report
owner: someone@example.com
semantic_layer:
  allowed_catalogs: [main]
params:
  - name: start_date
    type: DATE
    default: "2026-01-01"
blocks:
  - key: summary
    kind: kpi
    title: Summary
    identity: service_principal
    trust: certified
guardrails:
  max_rows: 100
"""
    sql = "-- @param start_date DATE\nSELECT 1 AS n FROM main.a.b WHERE d >= :start_date\n"
    problems = []
    with tempfile.TemporaryDirectory() as tmp:
        contract = Path(tmp) / "demo"
        (contract / "queries").mkdir(parents=True)
        (contract / "report.yaml").write_text(contract_yaml, encoding="utf-8")
        (contract / "queries" / "summary.sql").write_text(sql, encoding="utf-8")
        out = Path(tmp) / "demo-report"
        generate(contract, out, None, skip_validate=True)

        for rel in (
            "SKILL.md",
            "contract.manifest.json",
            "contract/report.yaml",
            "contract/queries/summary.sql",
            "scripts/run_report.py",
            "scripts/render_report.py",
            "assets/report.css",
            "assets/report-template.html",
            "assets/report-charts.js",
            "evals/evals.json",
        ):
            if not (out / rel).is_file():
                problems.append(f"missing generated file: {rel}")

        skill = (out / "SKILL.md").read_text(encoding="utf-8")
        if "{{" in skill:
            problems.append("SKILL.md still contains an unsubstituted {{TOKEN}}")
        if "v2.1.0" not in skill:
            problems.append("SKILL.md does not pin the contract version")
        desc = re.search(r"^description: (.*)$", skill, re.M)
        if not desc:
            problems.append("SKILL.md has no description")
        elif not DESC_MIN <= len(desc.group(1)) <= DESC_MAX:
            problems.append(
                f"description is {len(desc.group(1))} chars, outside {DESC_MIN}-{DESC_MAX}"
            )

        manifest = json.loads((out / "contract.manifest.json").read_text(encoding="utf-8"))
        if manifest["files"].get("queries/summary.sql") != hashlib.sha256(sql.encode()).hexdigest():
            problems.append("manifest hash does not match the materialized query")

        evals = json.loads((out / "evals" / "evals.json").read_text(encoding="utf-8"))
        counts = {
            t: sum(1 for c in evals["cases"] if c["type"] == t)
            for t in ("should_trigger", "should_not_trigger", "quality")
        }
        if (
            counts["should_trigger"] < 8
            or counts["should_not_trigger"] < 8
            or counts["quality"] < 3
        ):
            problems.append(f"generated evals are too thin: {counts}")

        # Drift must be detected: edit the copy and the runner has to refuse.
        (out / "contract" / "queries" / "summary.sql").write_text(
            sql + "-- edited\n", encoding="utf-8"
        )
        sys.path.insert(0, str(out / "scripts"))
        import run_report

        if not run_report.verify_manifest(out / "contract"):
            problems.append("a drifted contract copy was not detected by the manifest check")
        sys.path.pop(0)

        # Generating into the contract's own tree would delete the source before copying it.
        try:
            generate(contract, contract / "nested", None, skip_validate=True)
            problems.append("an overlapping --out was accepted — the contract could be destroyed")
        except SystemExit as exc:
            if exc.code != 2:
                problems.append(f"overlap check exited {exc.code}, expected 2")
        if not (contract / "queries" / "summary.sql").is_file():
            problems.append("the source contract was damaged by the overlap check")

        # A title carrying a command substitution or template token must be rejected outright.
        def gen_with_title(hostile: str) -> Path:
            bad = Path(tmp) / "hostile"
            (bad / "queries").mkdir(parents=True, exist_ok=True)
            (bad / "report.yaml").write_text(
                contract_yaml.replace("title: Demo Report", f"title: {json.dumps(hostile)}"),
                encoding="utf-8",
            )
            (bad / "queries" / "summary.sql").write_text(sql, encoding="utf-8")
            dest = Path(tmp) / "hostile-out"
            generate(bad, dest, None, skip_validate=True, force=True)
            return dest

        for hostile in ("Demo $(whoami)", "Demo {{TOKEN}}", "Demo `id`"):
            try:
                gen_with_title(hostile)
                problems.append(f"a hostile title was accepted: {hostile!r}")
            except SystemExit:
                pass

        # A newline or a colon collapses into the description, so the emitted scalar must be
        # quoted — otherwise YAML reads `description: a: b` as a nested mapping and the skill
        # silently never loads.
        for awkward in ("Demo\ndescription: pwned", "Q3: Revenue"):
            dest = gen_with_title(awkward)
            fm = (dest / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
            keys = [ln.split(":")[0] for ln in fm.splitlines() if ln and not ln[0].isspace()]
            if keys.count("description") != 1 or "name" not in keys:
                problems.append(f"title {awkward!r} corrupted the frontmatter keys: {keys}")
            desc_line = next(ln for ln in fm.splitlines() if ln.startswith("description:"))
            if not desc_line.partition(":")[2].strip().startswith('"'):
                problems.append(f"description was emitted unquoted for title {awkward!r}")

    if problems:
        for p in problems:
            print(f"SELFTEST FAIL: {p}", file=sys.stderr)
        return 1
    print("OK: self-test passed (materialization, hashing, drift detection, template, evals)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--contract", type=Path, help="validated contract directory")
    ap.add_argument(
        "--out", type=Path, help="skill folder to create (its name becomes the skill name)"
    )
    ap.add_argument("--validator", type=Path, help="path to validate_contract.py")
    ap.add_argument(
        "--skip-validate", action="store_true", help="only if the validator already exited 0"
    )
    ap.add_argument("--force", action="store_true", help="replace a non-empty output folder")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.contract or not args.out:
        ap.error("--contract and --out are required (or pass --selftest)")
    return generate(
        args.contract.resolve(),
        args.out.resolve(),
        find_validator(args.validator),
        args.skip_validate,
        args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
