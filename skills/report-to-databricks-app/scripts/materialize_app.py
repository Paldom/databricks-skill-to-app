#!/usr/bin/env python3
"""Materialize a governed report contract into an AppKit app, and gate drift.

Copies the contract's trusted queries into `config/queries/` byte-for-byte (preserving the
`.obo.sql` execution-identity suffix), binds its metric views into
`config/metric-views/definitions.json`, and records a SHA-256 manifest so CI can prove the app is
still running the contract it claims to.

Usage:
    python3 materialize_app.py --contract reports/monthly-pnl --app apps/pnl-app
    python3 materialize_app.py --contract reports/monthly-pnl --app apps/pnl-app --check
    python3 materialize_app.py --selftest

Exit codes: 0 = in sync (or written), 1 = drift detected / stale copy, 2 = cannot run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

MANIFEST_NAME = "report.manifest.json"
QUERY_REL_RE = re.compile(r"^queries/[A-Za-z0-9][A-Za-z0-9_.-]*\.sql$")
METRIC_REL = "metric-views/definitions.json"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(2)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_rel(rel: str) -> bool:
    """A manifest entry is a path we will copy to and delete from — validate it as untrusted.

    Without this, a key like `queries/../../../thing` or `queries//abs/path` escapes the app and
    the cleanup step deletes a file outside it.
    """
    if not isinstance(rel, str) or "\\" in rel or ".." in rel.split("/"):
        return False
    return bool(QUERY_REL_RE.match(rel)) or rel == METRIC_REL


def reject_symlinks(paths: list[Path]) -> None:
    for p in paths:
        if p.is_symlink():
            die(f"{p} is a symlink — refusing to follow it out of the managed tree")


def contract_files(contract: Path) -> dict[str, Path]:
    """Contract-relative name -> source path, for everything the app consumes."""
    out: dict[str, Path] = {}
    qdir = contract / "queries"
    if not qdir.is_dir():
        die(f"{qdir} not found — is this a contract directory?")
    files = sorted(qdir.glob("*.sql"))
    reject_symlinks(files)
    for f in files:
        rel = f"queries/{f.name}"
        if not safe_rel(rel):
            die(f"contract query {f.name!r} has a name that cannot be materialized safely")
        out[rel] = f
    # AppKit derives the query key from the filename, so x.sql and x.obo.sql are the same key
    # with two execution identities — an ambiguity the app resolves silently and wrongly.
    keys: dict[str, str] = {}
    for rel in out:
        name = rel.split("/", 1)[1]
        key = name[: -len(".obo.sql")] if name.endswith(".obo.sql") else name[: -len(".sql")]
        if key in keys:
            die(f"queries/{name} and queries/{keys[key]} both resolve to query key {key!r} — "
                "one block cannot have two execution identities")
        keys[key] = name
    mv = contract / "metric-views" / "definitions.json"
    if mv.is_file():
        reject_symlinks([mv])
        out[METRIC_REL] = mv
    return out


def app_query_files(app: Path) -> set[str]:
    """What is actually runnable in the app right now — not what a manifest claims."""
    qdir = app / "config" / "queries"
    found = {f"queries/{f.name}" for f in qdir.glob("*.sql")} if qdir.is_dir() else set()
    if (app / "config" / METRIC_REL).is_file():
        found.add(METRIC_REL)
    return found


def app_target(app: Path, rel: str) -> Path:
    if rel.startswith("queries/"):
        return app / "config" / "queries" / rel.split("/", 1)[1]
    return app / "config" / "metric-views" / "definitions.json"


def load_version(contract: Path) -> str:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        die("PyYAML is required to read report.yaml — `pip install pyyaml`")
    try:
        spec = yaml.safe_load((contract / "report.yaml").read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        die(f"{contract}/report.yaml is not valid YAML: {exc}")
    if not isinstance(spec, dict):
        die(f"{contract}/report.yaml must be a mapping")
    version = spec.get("version")
    # Inventing a version would let an unversioned contract look pinned.
    if not isinstance(version, (str, float, int)) or not re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
        die(f"{contract}/report.yaml has no valid semver `version` — the app pins this; "
            "run the governed-report-contract validator first")
    return str(version)


def load_manifest(manifest_path: Path) -> dict[str, str]:
    """Read and fully validate the manifest BEFORE anything is copied or deleted."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"{manifest_path} is unreadable: {exc}")
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        die(f"{manifest_path} has no `files` map")
    for rel in files:
        if not safe_rel(rel):
            die(f"{manifest_path} contains an unsafe entry {rel!r} — refusing to act on it")
    return {"__version__": str((data or {}).get("contract_version") or ""), **files}


def materialize(contract: Path, app: Path, check_only: bool) -> int:
    if not (contract / "report.yaml").is_file():
        die(f"{contract}/report.yaml not found")
    version = load_version(contract)
    sources = contract_files(contract)
    manifest_path = app / "config" / MANIFEST_NAME

    if check_only:
        if not manifest_path.is_file():
            print(f"FAIL: {manifest_path} missing — the app was never materialized from a contract",
                  file=sys.stderr)
            return 1
        recorded = load_manifest(manifest_path)
        pinned = recorded.pop("__version__")
        problems: list[str] = []
        if pinned != version:
            problems.append(f"app pins contract v{pinned} but the contract is now v{version}")
        for rel, src in sources.items():
            target = app_target(app, rel)
            if rel not in recorded:
                problems.append(f"{rel}: in the contract but not in the app manifest")
                continue
            if not target.is_file():
                problems.append(f"{rel}: missing from the app")
                continue
            digest = sha256(target)
            if digest != recorded[rel]:
                problems.append(f"{rel}: app copy edited by hand (sha256 {digest[:12]} != manifest {recorded[rel][:12]})")
            elif digest != sha256(src):
                problems.append(f"{rel}: app copy is stale — the contract moved on")
        # The decisive check: what the app can actually RUN, versus what the contract governs.
        # Comparing only against the manifest lets a hand-added query file pass unnoticed.
        for rel in sorted(app_query_files(app) - set(sources)):
            problems.append(f"{rel}: runnable in the app but not in the contract — ungoverned SQL")
        for rel in sorted(set(recorded) - set(sources)):
            problems.append(f"{rel}: in the app manifest but no longer in the contract")
        if problems:
            for p in problems:
                print(f"DRIFT {p}", file=sys.stderr)
            print(f"FAIL: {len(problems)} drift finding(s) — re-run without --check to re-materialize")
            return 1
        print(f"OK: app is in sync with contract v{version} ({len(sources)} file(s))")
        return 0

    # Everything below mutates the app, so validate the old manifest first — a manifest that
    # cannot be trusted must never drive a delete.
    previous = load_manifest(manifest_path) if manifest_path.is_file() else {}
    previous.pop("__version__", None)
    reject_symlinks([app_target(app, rel) for rel in {*previous, *sources}
                     if app_target(app, rel).exists()])

    files: dict[str, str] = {}
    for rel, src in sources.items():
        target = app_target(app, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        files[rel] = sha256(target)

    # Remove anything runnable that the contract no longer governs — including files this tool
    # never wrote, such as the scaffold's template queries.
    for rel in sorted((set(previous) | app_query_files(app)) - set(files)):
        stale = app_target(app, rel)
        if stale.is_file():
            stale.unlink()
            print(f"removed {stale.relative_to(app)} (not governed by the contract)")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        "contract": contract.name,
        "contract_version": version,
        "source": str(contract),
        "files": files,
    }, indent=2) + "\n", encoding="utf-8")

    obo = [r for r in files if r.endswith(".obo.sql")]
    print(f"OK: materialized contract v{version} into {app} "
          f"({len(files)} file(s), {len(obo)} on-behalf-of)")
    if obo:
        print("     per-user blocks: " + ", ".join(sorted(obo)))
    return 0


def selftest() -> int:
    import tempfile  # noqa: PLC0415

    problems: list[str] = []
    yaml_text = "version: 1.4.0\nname: demo\ntitle: Demo\nowner: o@example.com\n"
    with tempfile.TemporaryDirectory() as tmp:
        contract = Path(tmp) / "demo"
        (contract / "queries").mkdir(parents=True)
        (contract / "metric-views").mkdir(parents=True)
        (contract / "report.yaml").write_text(yaml_text, encoding="utf-8")
        (contract / "queries" / "a.sql").write_text("SELECT 1 AS n\n", encoding="utf-8")
        (contract / "queries" / "b.obo.sql").write_text("SELECT 2 AS n\n", encoding="utf-8")
        (contract / "metric-views" / "definitions.json").write_text(
            '{"metricViews": {"m": {"source": "main.s.v"}}}\n', encoding="utf-8")
        app = Path(tmp) / "app"

        if materialize(contract, app, False) != 0:
            problems.append("first materialization failed")
        if not (app / "config" / "queries" / "b.obo.sql").is_file():
            problems.append("the .obo.sql suffix was not preserved — per-user execution would be lost")
        if not (app / "config" / "metric-views" / "definitions.json").is_file():
            problems.append("metric-view definitions were not bound")
        if materialize(contract, app, True) != 0:
            problems.append("check mode reported drift immediately after materializing")

        # A hand-edited app copy must fail the gate.
        (app / "config" / "queries" / "a.sql").write_text("SELECT 999 AS n\n", encoding="utf-8")
        if materialize(contract, app, True) == 0:
            problems.append("a hand-edited app query did not trip the drift gate")

        # A moved-on contract must fail the gate too (stale copy, manifest still matches).
        materialize(contract, app, False)
        (contract / "queries" / "a.sql").write_text("SELECT 3 AS n\n", encoding="utf-8")
        if materialize(contract, app, True) == 0:
            problems.append("a stale app copy did not trip the drift gate")

        # A version bump alone must be visible.
        materialize(contract, app, False)
        (contract / "report.yaml").write_text(yaml_text.replace("1.4.0", "2.0.0"), encoding="utf-8")
        if materialize(contract, app, True) == 0:
            problems.append("a contract version bump did not trip the drift gate")

        # A dropped block must be removed from the app.
        materialize(contract, app, False)
        (contract / "queries" / "b.obo.sql").unlink()
        materialize(contract, app, False)
        if (app / "config" / "queries" / "b.obo.sql").is_file():
            problems.append("a query dropped from the contract still lingers in the app")

        # Ungoverned but runnable SQL must fail the gate: it is exactly how an OBO block gets
        # quietly replaced by a service-principal one.
        materialize(contract, app, False)
        (app / "config" / "queries" / "backdoor.sql").write_text("SELECT 1\n", encoding="utf-8")
        if materialize(contract, app, True) == 0:
            problems.append("a hand-added app query passed the drift gate")
        materialize(contract, app, False)
        if (app / "config" / "queries" / "backdoor.sql").is_file():
            problems.append("re-materializing did not remove ungoverned SQL")

        # A manifest is untrusted input: its keys become paths we copy to and delete from.
        canary = Path(tmp) / "canary.txt"
        canary.write_text("do not delete\n", encoding="utf-8")
        manifest = app / "config" / MANIFEST_NAME
        poisoned = json.loads(manifest.read_text(encoding="utf-8"))
        poisoned["files"]["queries/../../../canary.txt"] = "0" * 64
        manifest.write_text(json.dumps(poisoned), encoding="utf-8")
        for mode in (True, False):
            try:
                materialize(contract, app, mode)
                problems.append(f"a traversal manifest entry was accepted (check_only={mode})")
            except SystemExit as exc:
                if exc.code != 2:
                    problems.append(f"traversal rejection exited {exc.code}, expected 2")
        if not canary.is_file():
            problems.append("BLOCKER: a file outside the app was deleted via a manifest path")

        # Two files that resolve to the same AppKit query key must be refused, not silently merged.
        manifest.unlink()
        (contract / "queries" / "a.obo.sql").write_text("SELECT 1\n", encoding="utf-8")
        try:
            materialize(contract, app, False)
            problems.append("a.sql and a.obo.sql were both accepted for one query key")
        except SystemExit as exc:
            if exc.code != 2:
                problems.append(f"duplicate key rejection exited {exc.code}, expected 2")

    if problems:
        for p in problems:
            print(f"SELFTEST FAIL: {p}", file=sys.stderr)
        return 1
    print("OK: self-test passed (copy, obo suffix, hand-edit, stale copy, version bump, removal, "
          "ungoverned SQL, manifest path traversal, duplicate query key)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract", type=Path, help="contract directory (contains report.yaml)")
    ap.add_argument("--app", type=Path, help="AppKit app root (the folder containing config/)")
    ap.add_argument("--check", action="store_true", help="verify only; exit non-zero on drift (use in CI)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.contract or not args.app:
        ap.error("--contract and --app are required (or pass --selftest)")
    return materialize(args.contract.resolve(), args.app.resolve(), args.check)


if __name__ == "__main__":
    sys.exit(main())
