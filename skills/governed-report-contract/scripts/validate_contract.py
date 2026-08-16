#!/usr/bin/env python3
"""Validate a governed report contract.

Checks that report.yaml, queries/*.sql and metric-views/definitions.json form a contract whose
numbers are reproducible, and whose SQL survives BOTH execution paths (SQL Statement Execution
API and Databricks AppKit). See references/portability.md for why each rule exists.

Usage:
    python3 validate_contract.py reports/monthly-pnl      # validate one contract
    python3 validate_contract.py --selftest               # prove the rules still fire

Exit codes: 0 = valid, 1 = validation errors, 2 = cannot run (bad path, missing PyYAML).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PARAM_TYPES = {
    "STRING", "BOOLEAN", "DATE", "TIMESTAMP", "BINARY",
    "INT", "BIGINT", "TINYINT", "SMALLINT",
    "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL",
}
BLOCK_KINDS = {"kpi", "table", "chart", "narrative"}
IDENTITIES = {"service_principal", "user"}
TRUST = {"certified", "generated"}
EXECUTORS = {"app_service_principal", "user"}
SERVER_INJECTED = {"workspaceId"}

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PARAM_DECL_RE = re.compile(r"^\s*--\s*@param\s+(\w+)\s+(\w+(?:\([^)]*\))?)\s*(?:=\s*(.+?))?\s*$")
PARAM_USE_RE = re.compile(r"(?<![:\w]):(\w+)")
FQN_RE = re.compile(r"^[\w]+\.[\w]+\.[\w]+$")
# Statements that must never appear in a read-only contract query.
FORBIDDEN_RE = re.compile(
    r"\b(CREATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|CALL|GRANT|REVOKE|REFRESH|"
    r"COPY\s+INTO|SET\s+\w|USE\s+(CATALOG|SCHEMA|DATABASE))\b",
    re.IGNORECASE,
)
# String interpolation of any flavour: markers bind values, so an interpolated fragment is a hole.
INTERP_RE = re.compile(r"(\$\{|\{\{|%\(|%s\b|%d\b|\.format\(|\bf\"|\bf')")
RELATION_RE = re.compile(r"\b(?:FROM|JOIN)\s+([`\w.]+)", re.IGNORECASE)
CTE_RE = re.compile(r"(?:\bWITH\s+|,\s*)([`\w]+)\s+AS\s*\(", re.IGNORECASE)
LIMIT_PARAM_RE = re.compile(r"\b(?:LIMIT|OFFSET)\s+:(\w+)", re.IGNORECASE)
LIMIT_LITERAL_RE = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)
# Results that depend on who is asking must never land in a cache shared by everyone.
IDENTITY_FN_RE = re.compile(
    r"\b(current_user|session_user|is_member|is_account_group_member)\s*\(", re.IGNORECASE
)

errors: list[str] = []


def err(where: str, rule: str, msg: str) -> None:
    errors.append(f"ERROR {where} [{rule}]: {msg}")


def strip_sql(text: str) -> str:
    """Remove comments and string literals so pattern rules cannot misread them.

    Replaces each removed span with spaces to preserve offsets and word boundaries.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        if two == "--":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(" " * (j - i))
            i = j
        elif ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            # Backticked identifiers must survive: they are names, not literals.
            if quote == "`":
                out.append(text[i:j])
            else:
                out.append(" " * (j - i))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_params(text: str) -> tuple[dict[str, str], list[str]]:
    """Return ({name: TYPE}, [problems]) from `-- @param` annotation lines."""
    declared: dict[str, str] = {}
    problems: list[str] = []
    for line in text.splitlines():
        if "@param" not in line:
            continue
        m = PARAM_DECL_RE.match(line)
        if not m:
            problems.append(f"unparseable @param annotation: {line.strip()!r}")
            continue
        name, ptype = m.group(1), m.group(2).upper()
        base = ptype.split("(")[0]
        if base not in PARAM_TYPES:
            problems.append(f"@param {name}: unsupported type {ptype!r} (allowed: {', '.join(sorted(PARAM_TYPES))})")
        if name in declared:
            problems.append(f"@param {name} declared twice")
        declared[name] = base
    return declared, problems


def check_sql(path: Path, yaml_params: dict[str, str], allowed_catalogs: list[str],
              max_rows: int | None, require_total_order: bool, identity: str = "user") -> None:
    where = str(path)
    raw = path.read_text(encoding="utf-8")
    code = strip_sql(raw)

    declared, problems = parse_params(raw)
    for p in problems:
        err(where, "param-decl", p)

    body = code.strip().rstrip(";")
    if not body:
        err(where, "empty", "query file has no statement")
        return
    if ";" in body:
        err(where, "single-statement", "more than one statement — a contract query is exactly one read-only SELECT")
    if not re.match(r"^\s*(SELECT|WITH)\b", body, re.IGNORECASE):
        err(where, "read-only", "must start with SELECT or WITH")
    m = FORBIDDEN_RE.search(body)
    if m:
        err(where, "read-only", f"forbidden construct {m.group(0)!r} — contract queries never write or change session state")
    m = INTERP_RE.search(raw)
    if m:
        err(where, "no-interpolation", f"string interpolation {m.group(0)!r} — bind a :parameter instead; markers bind values, interpolation is an injection hole")
    if re.search(r"SELECT\s+\*", body, re.IGNORECASE):
        err(where, "no-select-star", "SELECT * — the output schema must be stable enough to type and render")

    used = {m.group(1) for m in PARAM_USE_RE.finditer(code)}
    for name in sorted(used - set(declared) - SERVER_INJECTED):
        err(where, "param-parity", f":{name} is used but not declared with `-- @param {name} <TYPE>`")
    for name in sorted(set(declared) - used):
        err(where, "param-parity", f"@param {name} is declared but never used")
    for name in sorted(SERVER_INJECTED & set(declared)):
        err(where, "server-injected", f":{name} is injected by the server and must not carry a @param annotation")

    for m in LIMIT_PARAM_RE.finditer(code):
        name = m.group(1)
        if declared.get(name) not in (None, "INT"):
            err(where, "limit-int", f":{name} caps LIMIT/OFFSET but is {declared[name]} — Spark requires IntegerType (INVALID_LIMIT_LIKE_EXPRESSION.DATA_TYPE); annotate INT")

    m = IDENTITY_FN_RE.search(code)
    if m and identity == "service_principal":
        err(where, "identity-cache", f"{m.group(1)}() makes the result depend on the caller, but this block runs as "
                                    "the service principal with a cache shared by every user — declare identity: user "
                                    "and rename the file to .obo.sql")

    has_limit = bool(re.search(r"\bLIMIT\b", code, re.IGNORECASE))
    if has_limit and require_total_order and not re.search(r"\bORDER\s+BY\b", code, re.IGNORECASE):
        err(where, "total-order", "LIMIT without ORDER BY returns an arbitrary sample, not the top N — order by the measure plus tie-breaker dimensions")
    if max_rows is not None:
        for m in LIMIT_LITERAL_RE.finditer(code):
            if int(m.group(1)) > max_rows:
                err(where, "max-rows", f"LIMIT {m.group(1)} exceeds guardrails.max_rows={max_rows}")

    ctes = {c.strip("`").lower() for c in CTE_RE.findall(code)}
    for rel in RELATION_RE.findall(code):
        clean = rel.replace("`", "")
        if clean.lower() in ctes or not clean or clean.startswith("("):
            continue
        if not FQN_RE.match(clean):
            err(where, "fully-qualified", f"relation {clean!r} is not catalog.schema.object — an unqualified name resolves against the session default")
            continue
        catalog = clean.split(".")[0]
        if allowed_catalogs and catalog not in allowed_catalogs:
            err(where, "allowed-catalogs", f"relation {clean!r} reads catalog {catalog!r}, which is not in semantic_layer.allowed_catalogs")

    for name, ptype in sorted(declared.items()):
        if name in yaml_params and yaml_params[name] != ptype:
            err(where, "param-parity", f"@param {name} is {ptype} but report.yaml declares {yaml_params[name]}")


def check_metric_views(path: Path, allowed_catalogs: list[str]) -> None:
    where = str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        err(where, "json", f"invalid JSON: {exc}")
        return
    views = data.get("metricViews")
    if not isinstance(views, dict) or not views:
        err(where, "schema", 'must be an object with a non-empty "metricViews" map')
        return
    for key, entry in views.items():
        if not isinstance(entry, dict):
            err(where, "schema", f"metricViews.{key} must be an object")
            continue
        source = entry.get("source")
        if not isinstance(source, str) or not FQN_RE.match(source):
            err(where, "fqn", f"metricViews.{key}.source must be a three-part catalog.schema.view FQN, got {source!r}")
        elif allowed_catalogs and source.split(".")[0] not in allowed_catalogs:
            err(where, "allowed-catalogs", f"metricViews.{key}.source reads catalog {source.split('.')[0]!r}, not in semantic_layer.allowed_catalogs")
        executor = entry.get("executor", "app_service_principal")
        if executor not in EXECUTORS:
            err(where, "executor", f"metricViews.{key}.executor must be one of {sorted(EXECUTORS)}, got {executor!r}")


def check_contract(root: Path) -> None:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("ERROR: PyYAML is required to read report.yaml — install it with `pip install pyyaml`", file=sys.stderr)
        raise SystemExit(2)

    manifest = root / "report.yaml"
    if not manifest.is_file():
        print(f"ERROR: {manifest} not found — a contract directory must contain report.yaml", file=sys.stderr)
        raise SystemExit(2)
    where = str(manifest)
    try:
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(where, "yaml", f"invalid YAML: {exc}")
        return
    if not isinstance(spec, dict):
        err(where, "yaml", "report.yaml must be a mapping")
        return

    for field in ("version", "name", "title", "owner", "blocks", "guardrails"):
        if not spec.get(field):
            err(where, "required", f"missing required field {field!r}")

    version = str(spec.get("version", ""))
    if version and not SEMVER_RE.match(version):
        err(where, "semver", f"version {version!r} must be MAJOR.MINOR.PATCH — consumers pin this")
    name = str(spec.get("name", ""))
    if name and not NAME_RE.match(name):
        err(where, "name", f"name {name!r} must be kebab-case")
    if name and name != root.name:
        err(where, "name", f"name {name!r} must equal the directory name {root.name!r}")

    semantic = spec.get("semantic_layer") or {}
    allowed_catalogs = semantic.get("allowed_catalogs") or []
    if not isinstance(allowed_catalogs, list) or not all(isinstance(c, str) for c in allowed_catalogs):
        err(where, "schema", "semantic_layer.allowed_catalogs must be a list of catalog names")
        allowed_catalogs = []
    if not allowed_catalogs:
        err(where, "allowed-catalogs", "semantic_layer.allowed_catalogs is required — it is the report's blast radius")

    guardrails = spec.get("guardrails") or {}
    max_rows = guardrails.get("max_rows")
    if not isinstance(max_rows, int) or max_rows <= 0:
        err(where, "guardrails", "guardrails.max_rows must be a positive integer")
        max_rows = None
    require_total_order = guardrails.get("require_total_order", True) is not False

    yaml_params: dict[str, str] = {}
    for i, p in enumerate(spec.get("params") or []):
        tag = f"params[{i}]"
        if not isinstance(p, dict) or not p.get("name") or not p.get("type"):
            err(where, "schema", f"{tag} needs both name and type")
            continue
        ptype = str(p["type"]).upper().split("(")[0]
        if ptype not in PARAM_TYPES:
            err(where, "param-type", f"{tag}: unsupported type {p['type']!r}")
        yaml_params[str(p["name"])] = ptype

    genie = spec.get("genie")
    if isinstance(genie, dict) and genie.get("trust") != "generated":
        err(where, "genie-trust", "genie.trust must be 'generated' — Genie's example SQL guides generation, it does not constrain it")

    queries = root / "queries"
    if not queries.is_dir():
        err(str(queries), "layout", "missing queries/ directory")
        return

    blocks = spec.get("blocks") or []
    if not isinstance(blocks, list) or not blocks:
        err(where, "blocks", "at least one block is required")
        blocks = []

    referenced: dict[Path, str] = {}
    freshness = (guardrails.get("freshness") or {}).get("watermark_block")
    keys = set()
    for i, b in enumerate(blocks):
        tag = f"blocks[{i}]"
        if not isinstance(b, dict):
            err(where, "schema", f"{tag} must be a mapping")
            continue
        key = b.get("key")
        if not key:
            err(where, "schema", f"{tag} needs a key")
            continue
        if key in keys:
            err(where, "schema", f"{tag}: duplicate block key {key!r}")
        keys.add(key)
        if b.get("kind") not in BLOCK_KINDS:
            err(where, "schema", f"{tag}: kind must be one of {sorted(BLOCK_KINDS)}, got {b.get('kind')!r}")
        identity = b.get("identity")
        if identity not in IDENTITIES:
            err(where, "schema", f"{tag}: identity must be one of {sorted(IDENTITIES)}, got {identity!r}")
        if b.get("trust") not in TRUST:
            err(where, "schema", f"{tag}: trust must be one of {sorted(TRUST)}, got {b.get('trust')!r}")

        plain, obo = queries / f"{key}.sql", queries / f"{key}.obo.sql"
        want = obo if identity == "user" else plain
        other = plain if identity == "user" else obo
        if not want.is_file():
            hint = (" — identity: user means the file must be named "
                    f"{key}.obo.sql so it executes on behalf of the signed-in user"
                    if identity == "user" else "")
            err(where, "identity-file", f"{tag}: expected {want.name} in queries/{hint}")
            if other.is_file():
                err(where, "identity-file", f"{tag}: found {other.name} instead — rename it to match the declared identity")
        else:
            referenced[want] = identity

    if freshness and freshness not in keys:
        err(where, "freshness", f"guardrails.freshness.watermark_block {freshness!r} is not a declared block")

    for f in sorted(queries.glob("*.sql")):
        if f not in referenced:
            err(str(f), "orphan", "no block in report.yaml references this query — unreferenced SQL is ungoverned")

    for f in sorted(referenced):
        check_sql(f, yaml_params, allowed_catalogs, max_rows, require_total_order, referenced[f])

    mv_rel = semantic.get("metric_views")
    if mv_rel:
        mv_path = root / mv_rel
        if not mv_path.is_file():
            err(str(mv_path), "layout", "semantic_layer.metric_views points at a file that does not exist")
        else:
            check_metric_views(mv_path, allowed_catalogs)


# --------------------------------------------------------------------------- self-test

SELFTEST_GOOD_YAML = """\
version: 1.0.0
name: good
title: Good report
owner: someone@example.com
semantic_layer:
  metric_views: metric-views/definitions.json
  allowed_catalogs: [main]
params:
  - name: start_date
    type: DATE
    default: "2026-01-01"
  - name: row_limit
    type: INT
    default: 50
blocks:
  - key: summary
    kind: kpi
    title: Summary
    identity: service_principal
    trust: certified
  - key: by_entity
    kind: table
    title: By entity
    identity: user
    trust: certified
guardrails:
  max_rows: 5000
  require_total_order: true
"""

SELFTEST_GOOD_SQL = """\
-- @param start_date DATE
-- @param row_limit INT
SELECT entity, SUM(amount) AS amount
FROM main.finance.pnl_fact
WHERE booked_on >= :start_date
GROUP BY entity
ORDER BY amount DESC, entity
LIMIT :row_limit
"""

SELFTEST_GOOD_OBO = """\
-- @param start_date DATE
SELECT entity, SUM(amount) AS amount   -- 100% of rows, uses a ':' in text
FROM main.finance.pnl_fact
WHERE booked_on >= :start_date
GROUP BY entity
ORDER BY entity
"""

SELFTEST_MV = """\
{"metricViews": {"pnl": {"source": "main.finance.pnl_metrics", "executor": "user"}}}
"""


def _write_contract(root: Path, yaml_text: str, sql: str, obo: str, mv: str) -> None:
    (root / "queries").mkdir(parents=True, exist_ok=True)
    (root / "metric-views").mkdir(parents=True, exist_ok=True)
    (root / "report.yaml").write_text(yaml_text, encoding="utf-8")
    (root / "queries" / "summary.sql").write_text(sql, encoding="utf-8")
    (root / "queries" / "by_entity.obo.sql").write_text(obo, encoding="utf-8")
    (root / "metric-views" / "definitions.json").write_text(mv, encoding="utf-8")


def selftest() -> int:
    """Every rule must fire on a deliberately broken contract and stay silent on a good one."""
    import tempfile  # noqa: PLC0415

    global errors
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "good"
        _write_contract(good, SELFTEST_GOOD_YAML, SELFTEST_GOOD_SQL, SELFTEST_GOOD_OBO, SELFTEST_MV)
        errors = []
        check_contract(good)
        if errors:
            failures.append("clean contract reported errors:\n  " + "\n  ".join(errors))

    # (label, sql mutation, expected rule tag)
    sql_cases = [
        ("undeclared param", SELFTEST_GOOD_SQL.replace("-- @param start_date DATE\n", ""), "param-parity"),
        ("unused param", SELFTEST_GOOD_SQL + "\n-- @param unused STRING\n", "param-parity"),
        ("limit as bigint", SELFTEST_GOOD_SQL.replace("row_limit INT", "row_limit BIGINT"), "limit-int"),
        ("limit without order by", SELFTEST_GOOD_SQL.replace("ORDER BY amount DESC, entity\n", ""), "total-order"),
        ("select star", SELFTEST_GOOD_SQL.replace("SELECT entity, SUM(amount) AS amount", "SELECT *"), "no-select-star"),
        ("interpolation", SELFTEST_GOOD_SQL.replace(":start_date", "'${start_date}'"), "no-interpolation"),
        ("write statement", SELFTEST_GOOD_SQL + "\n;DROP TABLE main.finance.pnl_fact\n", "read-only"),
        ("unqualified relation", SELFTEST_GOOD_SQL.replace("main.finance.pnl_fact", "pnl_fact"), "fully-qualified"),
        ("foreign catalog", SELFTEST_GOOD_SQL.replace("main.finance.pnl_fact", "prod.finance.pnl_fact"), "allowed-catalogs"),
        ("bad param type", SELFTEST_GOOD_SQL.replace("start_date DATE", "start_date VARCHAR"), "param-decl"),
        ("server-injected annotated", SELFTEST_GOOD_SQL.replace("-- @param row_limit INT", "-- @param row_limit INT\n-- @param workspaceId STRING") + "\nAND ws = :workspaceId", "server-injected"),
        ("literal over cap", SELFTEST_GOOD_SQL.replace("LIMIT :row_limit", "LIMIT 99999"), "max-rows"),
        ("type disagreement", SELFTEST_GOOD_SQL.replace("start_date DATE", "start_date STRING"), "param-parity"),
        ("identity fn on shared cache", SELFTEST_GOOD_SQL.replace("WHERE booked_on >= :start_date", "WHERE booked_on >= :start_date AND owner = current_user()"), "identity-cache"),
        ("dynamic relation identifier", SELFTEST_GOOD_SQL.replace("FROM main.finance.pnl_fact", "FROM IDENTIFIER(:start_date)"), "fully-qualified"),
        ("write hidden after a CTE", SELFTEST_GOOD_SQL.replace("SELECT entity,", "WITH x AS (SELECT 1) DELETE FROM main.finance.pnl_fact; SELECT entity,"), "read-only"),
    ]
    for label, sql, rule in sql_cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "good"
            _write_contract(root, SELFTEST_GOOD_YAML, sql, SELFTEST_GOOD_OBO, SELFTEST_MV)
            errors = []
            check_contract(root)
            if not any(f"[{rule}]" in e for e in errors):
                failures.append(f"{label}: expected rule [{rule}] to fire, got: {errors or 'no errors'}")

    yaml_cases = [
        ("identity/file mismatch", SELFTEST_GOOD_YAML.replace("    identity: user", "    identity: service_principal"), "identity-file"),
        ("bad semver", SELFTEST_GOOD_YAML.replace("version: 1.0.0", "version: v1"), "semver"),
        ("name mismatch", SELFTEST_GOOD_YAML.replace("name: good", "name: other"), "name"),
        ("no allowed catalogs", SELFTEST_GOOD_YAML.replace("  allowed_catalogs: [main]", "  allowed_catalogs: []"), "allowed-catalogs"),
        ("bad kind", SELFTEST_GOOD_YAML.replace("    kind: kpi", "    kind: gauge"), "schema"),
        ("genie trust", SELFTEST_GOOD_YAML + "genie:\n  space_id: abc\n  trust: certified\n", "genie-trust"),
        ("missing owner", SELFTEST_GOOD_YAML.replace("owner: someone@example.com\n", ""), "required"),
        ("bad max_rows", SELFTEST_GOOD_YAML.replace("  max_rows: 5000", "  max_rows: 0"), "guardrails"),
        ("unknown watermark", SELFTEST_GOOD_YAML + "  freshness:\n    watermark_block: nope\n", "freshness"),
    ]
    for label, text, rule in yaml_cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "good"
            _write_contract(root, text, SELFTEST_GOOD_SQL, SELFTEST_GOOD_OBO, SELFTEST_MV)
            errors = []
            check_contract(root)
            if not any(f"[{rule}]" in e for e in errors):
                failures.append(f"{label}: expected rule [{rule}] to fire, got: {errors or 'no errors'}")

    mv_cases = [
        ("two-part fqn", '{"metricViews": {"pnl": {"source": "finance.pnl_metrics"}}}', "fqn"),
        ("bad executor", '{"metricViews": {"pnl": {"source": "main.finance.pnl_metrics", "executor": "admin"}}}', "executor"),
        ("not json", "{nope", "json"),
    ]
    for label, mv, rule in mv_cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "good"
            _write_contract(root, SELFTEST_GOOD_YAML, SELFTEST_GOOD_SQL, SELFTEST_GOOD_OBO, mv)
            errors = []
            check_contract(root)
            if not any(f"[{rule}]" in e for e in errors):
                failures.append(f"{label}: expected rule [{rule}] to fire, got: {errors or 'no errors'}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "good"
        _write_contract(root, SELFTEST_GOOD_YAML, SELFTEST_GOOD_SQL, SELFTEST_GOOD_OBO, SELFTEST_MV)
        (root / "queries" / "stray.sql").write_text("SELECT 1", encoding="utf-8")
        errors = []
        check_contract(root)
        if not any("[orphan]" in e for e in errors):
            failures.append(f"orphan query: expected rule [orphan] to fire, got: {errors or 'no errors'}")

    # False positives are as damaging as misses: a keyword in a comment or a semicolon inside a
    # string literal must not trip the pattern rules.
    tricky = SELFTEST_GOOD_SQL.replace(
        "SELECT entity, SUM(amount) AS amount",
        "SELECT entity, SUM(amount) AS amount, 'a;b DROP TABLE x' AS note  -- INSERT INTO not really",
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "good"
        _write_contract(root, SELFTEST_GOOD_YAML, tricky, SELFTEST_GOOD_OBO, SELFTEST_MV)
        errors = []
        check_contract(root)
        if errors:
            failures.append("false positive on keywords inside a comment/string literal:\n  " + "\n  ".join(errors))

    if failures:
        for f in failures:
            print(f"SELFTEST FAIL: {f}", file=sys.stderr)
        print(f"FAIL: {len(failures)} self-test failure(s)")
        return 1
    print(f"OK: self-test passed ({len(sql_cases) + len(yaml_cases) + len(mv_cases) + 2} cases)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("contract", nargs="?", type=Path, help="path to a contract directory (containing report.yaml)")
    ap.add_argument("--selftest", action="store_true", help="verify the rules still fire, then exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.contract:
        ap.error("a contract directory is required (or pass --selftest)")
    if not args.contract.is_dir():
        print(f"ERROR: {args.contract} is not a directory", file=sys.stderr)
        return 2

    check_contract(args.contract.resolve())
    for line in errors:
        print(line, file=sys.stderr)
    print(f"{'FAIL' if errors else 'OK'}: {len(errors)} error(s) in {args.contract}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
