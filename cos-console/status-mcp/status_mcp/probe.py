"""CLI harness — print a StatusReport as JSON without an MCP client.

    python -m status_mcp.probe dstrader
    python -m status_mcp.probe --project dstrader --pretty
    python -m status_mcp.probe --list
    python -m status_mcp.probe dstrader --validate   # check against JSON Schema

Pure stdlib (no `mcp` needed), so it dry-runs anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import list_projects
from .report import build_status_report


def _validate(report: dict) -> tuple[bool, str]:
    schema_path = Path(__file__).resolve().parent.parent / "status_report.schema.json"
    try:
        import jsonschema  # optional dev dep
    except ImportError:
        return True, "jsonschema not installed — skipped (pip install jsonschema to enable)"
    try:
        schema = json.loads(schema_path.read_text())
        jsonschema.validate(report, schema)
        return True, "valid against status_report.schema.json"
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        return False, f"SCHEMA VIOLATION: {e.message} (at {'/'.join(map(str, e.path))})"
    except OSError as e:
        return True, f"schema file unreadable ({e}) — skipped"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="status_mcp.probe",
                                 description="Print a StatusReport JSON for a project.")
    ap.add_argument("project", nargs="?", help="project key (e.g. dstrader)")
    ap.add_argument("--project", dest="project_opt", help="alternative to positional")
    ap.add_argument("--list", action="store_true", help="list known projects and exit")
    ap.add_argument("--pretty", action="store_true", help="indent JSON output")
    ap.add_argument("--validate", action="store_true", help="validate against the JSON Schema")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(list_projects()))
        return 0

    project = args.project or args.project_opt
    if not project:
        ap.error("a project key is required (e.g. `dstrader`), or use --list")

    try:
        report = build_status_report(project)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2 if args.pretty else None, ensure_ascii=False))

    if args.validate:
        ok, msg = _validate(report)
        print(f"[validate] {msg}", file=sys.stderr)
        if not ok:
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
