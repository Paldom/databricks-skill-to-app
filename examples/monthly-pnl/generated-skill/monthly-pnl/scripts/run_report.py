#!/usr/bin/env python3
"""Execute a governed report contract and emit a result envelope.

Runs each block's trusted query through the SQL Statement Execution API using the Databricks
CLI (so authentication is the CLI profile — this script never handles a token), then writes a
single normalized JSON envelope that the renderer consumes.

Usage:
    python3 run_report.py --contract ./contract --profile DEFAULT --warehouse <ID> \
        --param start_date=2026-01-01 --param row_limit=50 --out report.json

Exit codes: 0 = every block succeeded, 1 = at least one block failed (envelope still written,
failures are marked), 2 = cannot run (bad contract, missing CLI, missing PyYAML).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PARAM_DECL_RE = re.compile(r"^\s*--\s*@param\s+(\w+)\s+(\w+(?:\([^)]*\))?)", re.MULTILINE)
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}
# Verified against a live workspace: POST /api/2.0/sql/statements/ works;
# /api/2.0/sql/statements/execute returns "No API found for 'POST /sql/statements/execute'".
STATEMENTS = "/api/2.0/sql/statements/"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(2)


def cli_json(args: list[str], payload: dict | None = None) -> dict:
    """Call the Databricks CLI and parse its JSON response."""
    cmd = ["databricks", *args]
    if payload is not None:
        cmd += ["--json", json.dumps(payload)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("the `databricks` CLI is not on PATH — install it and authenticate a profile first")
    if proc.returncode != 0:
        die(f"databricks {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        die(f"databricks {' '.join(args)} returned non-JSON output: {proc.stdout[:400]!r}")
    return {}  # unreachable, keeps type checkers happy


def declared_params(sql: str) -> dict[str, str]:
    return {m.group(1): m.group(2).upper() for m in PARAM_DECL_RE.finditer(sql)}


def verify_manifest(contract: Path) -> list[str]:
    """Re-hash the materialized contract. A drifted copy is not the contract it claims to be.

    Fails closed: a missing or unreadable manifest is itself a finding. This detects accidental
    drift and stale copies — it is not a defence against someone who can edit both the files and
    the manifest.
    """
    manifest = contract.parent / "contract.manifest.json"
    if not manifest.is_file():
        return [f"{manifest.name}: missing — this contract copy is unpinned; re-generate the skill"]
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{manifest.name}: unreadable ({exc})"]
    recorded = data.get("files")
    if not isinstance(recorded, dict) or not recorded:
        return [f"{manifest.name}: no file hashes recorded"]

    problems = []
    for rel, expected in sorted(recorded.items()):
        f = contract / rel
        if not f.is_file():
            problems.append(f"{rel}: missing")
            continue
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != expected:
            problems.append(f"{rel}: sha256 {actual[:12]} != manifest {expected[:12]}")
    on_disk = {str(p.relative_to(contract)) for p in contract.rglob("*") if p.is_file()}
    for rel in sorted(on_disk - set(recorded)):
        problems.append(f"{rel}: present in the copy but not in the manifest")
    return problems


def with_profile(args: list[str], profile: str) -> list[str]:
    return [*args, "--profile", profile] if profile else args


def execute(
    profile: str,
    warehouse: str,
    statement: str,
    parameters: list[dict],
    timeout_s: int,
    row_cap: int | None = None,
) -> dict:
    payload = {
        "warehouse_id": warehouse,
        "statement": statement,
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
    }
    if parameters:
        payload["parameters"] = parameters
    if row_cap:
        # Ask the server for a bounded result instead of discovering the size afterwards.
        payload["row_limit"] = row_cap
    resp = cli_json(with_profile(["api", "post", STATEMENTS], profile), payload)

    deadline = time.monotonic() + timeout_s
    while (resp.get("status") or {}).get("state") not in TERMINAL:
        if time.monotonic() > deadline:
            sid = resp.get("statement_id", "")
            subprocess.run(
                [
                    "databricks",
                    *with_profile(["api", "post", f"{STATEMENTS}{sid}/cancel"], profile),
                ],
                capture_output=True,
                text=True,
            )
            return {
                "status": {
                    "state": "CANCELED",
                    "error": {"message": f"timed out after {timeout_s}s"},
                }
            }
        time.sleep(2)
        resp = cli_json(
            with_profile(["api", "get", f"{STATEMENTS}{resp['statement_id']}"], profile)
        )

    # Results arrive in chunks. Stopping at the first one silently drops rows, which is the
    # one failure a report must never have.
    if (resp.get("status") or {}).get("state") == "SUCCEEDED":
        result = resp.get("result") or {}
        rows = list(result.get("data_array") or [])
        link = result.get("next_chunk_internal_link")
        seen = 0
        while link and seen < 1000:
            seen += 1
            chunk = cli_json(with_profile(["api", "get", link], profile))
            rows.extend(chunk.get("data_array") or [])
            link = chunk.get("next_chunk_internal_link")
        resp.setdefault("result", {})["data_array"] = rows
    return resp


def coerce_params(declared: dict[str, str], values: dict[str, str], key: str) -> list[dict]:
    out = []
    for name, ptype in sorted(declared.items()):
        if name not in values:
            die(
                f"block {key}: no value for :{name} — pass --param {name}=<value> or give it a default in report.yaml"
            )
        # Values cross the wire as typed strings in both execution paths; keep them strings
        # so DECIMAL and large BIGINT never round-trip through a float.
        out.append({"name": name, "value": str(values[name]), "type": ptype})
    return out


FAKE_CLI = r'''#!/usr/bin/env python3
"""Stand-in for the databricks CLI: records calls and replays fixtures."""
import json, os, sys

args = sys.argv[1:]
log = os.environ["FAKE_CLI_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps(args) + "\n")

path = args[2] if len(args) > 2 else ""
if args[:2] == ["api", "post"] and path.endswith("/cancel"):
    print("{}")
    sys.exit(0)
if args[:2] == ["api", "post"]:
    body = json.loads(args[args.index("--json") + 1])
    if "current_user" in body["statement"]:
        print(json.dumps({"statement_id": "s0", "status": {"state": "SUCCEEDED"},
                          "manifest": {"schema": {"columns": [{"name": "principal", "type_name": "STRING"}]}},
                          "result": {"data_array": [["someone@example.com"]]}}))
    else:
        # First response is still RUNNING: the runner must poll.
        print(json.dumps({"statement_id": "s1", "status": {"state": "RUNNING"}}))
    sys.exit(0)
if args[:2] == ["api", "get"] and "/result/chunks/1" in path:
    print(json.dumps({"data_array": [["c", "3"]]}))
    sys.exit(0)
if args[:2] == ["api", "get"]:
    print(json.dumps({
        "statement_id": "s1", "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": [{"name": "k", "type_name": "STRING"},
                                            {"name": "v", "type_name": "DECIMAL(18,2)"}]},
                     "total_row_count": 3, "truncated": False},
        "result": {"data_array": [["a", "1"], ["b", "2"]],
                   "next_chunk_internal_link": "/api/2.0/sql/statements/s1/result/chunks/1"}}))
    sys.exit(0)
print("unexpected call: " + json.dumps(args), file=sys.stderr)
sys.exit(1)
'''


def selftest() -> int:
    """Exercise polling, chunk pagination, caps and parameter rejection without a warehouse."""
    import os
    import tempfile

    problems: list[str] = []
    contract_yaml = """\
version: 1.0.0
name: demo
title: Demo
owner: o@example.com
params:
  - name: start_date
    type: DATE
    default: "2026-01-01"
  - name: row_limit
    type: INT
    default: 2
    max: 10
blocks:
  - key: summary
    kind: table
    title: Summary
    identity: service_principal
    trust: certified
guardrails:
  max_rows: 100
"""
    sql = "-- @param start_date DATE\nSELECT k, v FROM main.a.b WHERE d >= :start_date\n"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        contract = root / "skill" / "contract"
        (contract / "queries").mkdir(parents=True)
        (contract / "report.yaml").write_text(contract_yaml, encoding="utf-8")
        (contract / "queries" / "summary.sql").write_text(sql, encoding="utf-8")
        files = {
            str(p.relative_to(contract)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(contract.rglob("*"))
            if p.is_file()
        }
        (root / "skill" / "contract.manifest.json").write_text(
            json.dumps({"contract": "demo", "version": "1.0.0", "files": files}), encoding="utf-8"
        )

        bindir = root / "bin"
        bindir.mkdir()
        fake = bindir / "databricks"
        fake.write_text(FAKE_CLI, encoding="utf-8")
        fake.chmod(0o755)
        log = root / "calls.log"
        os.environ["FAKE_CLI_LOG"] = str(log)
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"

        out = root / "envelope.json"
        rc = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--contract",
                str(contract),
                "--warehouse",
                "wh1",
                "--profile",
                "P",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            problems.append(f"clean run failed: {rc.stderr.strip()}")
        else:
            env = json.loads(out.read_text(encoding="utf-8"))
            block = env["blocks"][0]
            if block.get("row_count") != 3:
                problems.append(
                    f"chunk pagination lost rows: got {block.get('row_count')}, expected 3"
                )
            if env.get("attested_principal") != "someone@example.com":
                problems.append("principal was not attested")
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            posts = [c for c in calls if c[:2] == ["api", "post"]]
            if not posts or posts[0][2] != STATEMENTS:
                problems.append(f"wrong execute endpoint: {posts[0][2] if posts else 'none'}")
            if not any(c[:2] == ["api", "get"] for c in calls):
                problems.append("did not poll a non-terminal statement")
            if not all("--profile" in c for c in calls):
                problems.append("a call was made without the requested profile")

        # An undeclared parameter must be rejected, not silently ignored.
        rc = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--contract",
                str(contract),
                "--warehouse",
                "wh1",
                "--param",
                "startdate=2026-01-01",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 2 or "not declared" not in rc.stderr:
            problems.append("an undeclared --param was not rejected")

        # A parameter above its declared bound must be rejected before anything runs.
        rc = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--contract",
                str(contract),
                "--warehouse",
                "wh1",
                "--param",
                "row_limit=9999",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 2 or "exceeds the contract bound" not in rc.stderr:
            problems.append("an out-of-bounds --param was not rejected")

        # A drifted copy must fail closed.
        (contract / "queries" / "summary.sql").write_text(sql + "-- edit\n", encoding="utf-8")
        rc = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--contract",
                str(contract),
                "--warehouse",
                "wh1",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 2 or "manifest" not in rc.stderr:
            problems.append("a drifted contract copy was not refused")

    if problems:
        for p in problems:
            print(f"SELFTEST FAIL: {p}", file=sys.stderr)
        return 1
    print("OK: self-test passed (endpoint, polling, chunking, attestation, param rejection, drift)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    if "--selftest" in sys.argv:
        return selftest()
    ap.add_argument(
        "--contract", type=Path, required=True, help="contract directory (contains report.yaml)"
    )
    ap.add_argument("--profile", default="", help="Databricks CLI profile (never auto-selected)")
    ap.add_argument("--warehouse", required=True, help="SQL warehouse id")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--out", type=Path, required=True, help="where to write the result envelope")
    ap.add_argument("--timeout", type=int, default=300, help="per-block timeout in seconds")
    args = ap.parse_args()

    try:
        import yaml
    except ImportError:
        die("PyYAML is required to read report.yaml — `pip install pyyaml`")

    contract = args.contract.resolve()
    manifest = contract / "report.yaml"
    if not manifest.is_file():
        die(f"{manifest} not found")
    spec = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}

    drift = verify_manifest(contract)
    if drift:
        die(
            "materialized contract does not match its manifest — re-materialize instead of editing "
            "the copy:\n  " + "\n  ".join(drift)
        )

    declared_in_yaml = {str(p["name"]) for p in (spec.get("params") or []) if p.get("name")}
    values = {
        p["name"]: p.get("default")
        for p in (spec.get("params") or [])
        if p.get("default") is not None
    }
    seen_overrides: set[str] = set()
    for kv in args.param:
        if "=" not in kv:
            die(f"--param expects NAME=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        # A typo'd parameter name must not silently leave the default in force.
        if k not in declared_in_yaml:
            die(
                f"--param {k}: not declared in report.yaml (declared: {', '.join(sorted(declared_in_yaml)) or 'none'})"
            )
        if k in seen_overrides:
            die(f"--param {k}: given more than once")
        seen_overrides.add(k)
        values[k] = v

    # Bounds are enforced before anything runs: a row cap checked after the fact has already
    # paid for the scan and already moved the rows.
    for p in spec.get("params") or []:
        name, bound = p.get("name"), p.get("max")
        if bound is None or name not in values:
            continue
        try:
            if float(values[name]) > float(bound):
                die(f"--param {name}={values[name]} exceeds the contract bound max={bound}")
        except (TypeError, ValueError):
            die(
                f"--param {name}={values[name]!r} is not comparable with the declared max={bound!r}"
            )

    guardrails = spec.get("guardrails") or {}
    max_rows = guardrails.get("max_rows")
    freshness_cfg = guardrails.get("freshness") or {}

    envelope = {
        "contract": {
            "name": spec.get("name"),
            "version": spec.get("version"),
            "title": spec.get("title"),
            "owner": spec.get("owner"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warehouse_id": args.warehouse,
        "profile": args.profile or "(default resolution)",
        "params": values,
        "blocks": [],
    }

    # Attest, don't assume. A block declared `identity: user` only really runs as that user when
    # the profile's credential is that user — the filename convention binds inside AppKit, not here.
    principal = execute(args.profile, args.warehouse, "SELECT current_user() AS principal", [], 60)
    rows = (principal.get("result") or {}).get("data_array") or []
    envelope["attested_principal"] = (
        rows[0][0] if (principal.get("status") or {}).get("state") == "SUCCEEDED" and rows else None
    )

    failed = 0
    for block in spec.get("blocks") or []:
        key, identity = block.get("key"), block.get("identity")
        sql_path = contract / "queries" / (f"{key}.obo.sql" if identity == "user" else f"{key}.sql")
        entry = {
            "key": key,
            "kind": block.get("kind"),
            "title": block.get("title"),
            "identity": identity,
            "trust": block.get("trust"),
            "status": "ok",
        }
        if not sql_path.is_file():
            entry.update(status="error", error=f"missing query file {sql_path.name}")
            envelope["blocks"].append(entry)
            failed += 1
            continue

        sql = sql_path.read_text(encoding="utf-8")
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        resp = execute(
            args.profile,
            args.warehouse,
            sql,
            coerce_params(declared_params(sql), values, key),
            args.timeout,
            row_cap=max_rows,
        )
        state = (resp.get("status") or {}).get("state")
        entry["executed_at"] = started

        if state != "SUCCEEDED":
            entry.update(
                status="error",
                error=((resp.get("status") or {}).get("error") or {}).get(
                    "message", state or "unknown"
                ),
            )
            failed += 1
        else:
            res = resp.get("result") or {}
            man = resp.get("manifest") or {}
            rows = res.get("data_array") or []
            entry["columns"] = [
                {"name": c.get("name"), "type": c.get("type_name")}
                for c in ((man.get("schema") or {}).get("columns") or [])
            ]
            # Never present a truncated result as a complete report.
            if max_rows and len(rows) > max_rows:
                rows = rows[:max_rows]
                entry["status"] = "partial"
                entry["error"] = f"capped at guardrails.max_rows={max_rows}"
            if man.get("truncated"):
                entry["truncated"] = True
                entry["status"] = "partial"
                entry.setdefault("error", "the server truncated this result")
            entry["rows"] = rows
            entry["row_count"] = len(rows)
            if man.get("total_row_count") is not None:
                entry["total_row_count"] = man["total_row_count"]
            if not rows:
                entry["status"] = "empty"
        envelope["blocks"].append(entry)

    watermark_block = freshness_cfg.get("watermark_block")
    if watermark_block:
        wm = next((b for b in envelope["blocks"] if b["key"] == watermark_block), None)
        value = wm["rows"][0][0] if wm and wm.get("rows") and wm["rows"][0] else None
        envelope["freshness"] = {
            "watermark": value,
            "max_lag": freshness_cfg.get("max_lag"),
            "source_block": watermark_block,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    print(
        f"{'FAIL' if failed else 'OK'}: {len(envelope['blocks'])} block(s), {failed} failed -> {args.out}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
