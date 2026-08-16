#!/usr/bin/env python3
"""Create the example's semantic layer in a Databricks workspace.

Runs the numbered .sql files in this directory through the SQL Statement Execution API using the
Databricks CLI, so authentication is the CLI profile and no token is handled here.

Usage:
    python3 deploy_semantic_layer.py --profile DEFAULT --warehouse <ID> \
        [--catalog demo] [--schema pnl_demo]

Exit codes: 0 = deployed, 1 = a statement failed, 2 = cannot run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

STATEMENTS = "/api/2.0/sql/statements/"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


def run_sql(profile: str, warehouse: str, statement: str, timeout_s: int = 600) -> dict:
    cmd = ["databricks", "api", "post", STATEMENTS, "--profile", profile, "--json", json.dumps({
        "warehouse_id": warehouse, "statement": statement,
        "format": "JSON_ARRAY", "disposition": "INLINE",
        "wait_timeout": "30s", "on_wait_timeout": "CONTINUE",
    })]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"ERROR: CLI failed: {proc.stderr.strip() or proc.stdout.strip()}", file=sys.stderr)
        raise SystemExit(2)
    resp = json.loads(proc.stdout)

    deadline = time.monotonic() + timeout_s
    while (resp.get("status") or {}).get("state") not in TERMINAL:
        if time.monotonic() > deadline:
            return {"status": {"state": "CANCELED", "error": {"message": "timed out"}}}
        time.sleep(3)
        got = subprocess.run(
            ["databricks", "api", "get", f"{STATEMENTS}{resp['statement_id']}", "--profile", profile],
            capture_output=True, text=True)
        resp = json.loads(got.stdout)
    return resp


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="Databricks CLI profile (never auto-selected)")
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--catalog", default="demo")
    ap.add_argument("--schema", default="pnl_demo")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    fqn = f"{args.catalog}.{args.schema}"
    steps: list[tuple[str, str]] = [
        ("create schema", f"CREATE SCHEMA IF NOT EXISTS {fqn} "
                          f"COMMENT 'Worked example for databricks-skill-to-app'"),
    ]
    for f in sorted(here.glob("*.sql")):
        sql = f.read_text(encoding="utf-8").replace("demo.pnl_demo", fqn)
        steps.append((f.name, sql))
    steps.append(("verify metric view",
                  f"SELECT `Booked Month`, MEASURE(`Revenue`) AS revenue, "
                  f"MEASURE(`Gross Margin Pct`) AS margin_pct "
                  f"FROM {fqn}.pnl_metrics GROUP BY ALL ORDER BY `Booked Month` DESC LIMIT 3"))

    for label, sql in steps:
        resp = run_sql(args.profile, args.warehouse, sql)
        state = (resp.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            msg = ((resp.get("status") or {}).get("error") or {}).get("message", state)
            print(f"FAIL [{label}]: {msg}", file=sys.stderr)
            return 1
        rows = (resp.get("result") or {}).get("data_array") or []
        print(f"OK   [{label}]" + (f" -> {rows}" if rows else ""))

    print(f"\nDeployed {fqn}.pnl_fact and {fqn}.pnl_metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
