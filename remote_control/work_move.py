"""``python3 -m remote_control work move`` -- move agent work between engines.

The two cross-engine directions are asymmetric, because the platforms are:

  * **Codex -> Claude** is fully automatable: convert a Codex rollout into a
    resumable Claude Code session. This delegates to the proven importer
    (:mod:`session_port`), so it is dry-run by default; pass ``--write``.
  * **Claude -> Codex** is an *app-only* feature ("import external agent session"
    in the Codex app, recorded in ``~/.codex/external_agent_session_imports.json``).
    The CLI cannot perform it, so this direction resolves the Claude session file
    to import and reports whether Codex has *already* imported it -- a guide +
    status, not an action.

This is the **migrate** slice of the cross-engine work-orchestration epic
(trigger / inventory / stale-detect / migrate); it wraps existing machinery
rather than reinventing it. The pure helpers (import-records parsing, status
formatting, direction resolution) live above ``main`` and unit-test against
plain strings; the file globbing + delegation live in ``main``.
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import DEV

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
EXTERNAL_IMPORTS = Path.home() / ".codex" / "external_agent_session_imports.json"

ENGINES = ("codex", "claude")
_OPPOSITE = {"codex": "claude", "claude": "codex"}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def resolve_direction(from_engine: Optional[str], to_engine: Optional[str]):
    """``(from, to)`` for a cross-engine move, or raise ``ValueError``.

    *to* defaults to the opposite of *from*; only the two cross-engine pairs are
    valid (a same-engine move is :mod:`session_fork`'s job, not this)."""
    if from_engine not in ENGINES:
        raise ValueError(f"--from must be codex or claude, got {from_engine!r}")
    to_engine = to_engine or _OPPOSITE[from_engine]
    if to_engine not in ENGINES:
        raise ValueError(f"--to must be codex or claude, got {to_engine!r}")
    if to_engine == from_engine:
        raise ValueError("--from and --to must differ (same-engine clone is `fork`)")
    return from_engine, to_engine


def parse_external_imports(text: str) -> Dict[str, dict]:
    """``external_agent_session_imports.json`` text -> ``{source_path: record}``.

    Each record is the imported Claude session (``imported_thread_id`` +
    ``imported_at`` epoch). Tolerates malformed/empty content (returns {})."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return {}
    out: Dict[str, dict] = {}
    for rec in (obj.get("records") or []) if isinstance(obj, dict) else []:
        if isinstance(rec, dict) and rec.get("source_path"):
            out[rec["source_path"]] = rec
    return out


def _fmt_epoch(epoch) -> str:
    try:
        return f"{datetime.fromtimestamp(float(epoch), timezone.utc):%Y-%m-%d %H:%M}Z"
    except (TypeError, ValueError, OSError):
        return "?"


def format_import_status(path: str, record: Optional[dict]) -> str:
    """A human line for one Claude session's Claude->Codex import status."""
    if record:
        tid = record.get("imported_thread_id", "?")
        return (f"already imported -> Codex thread {tid} "
                f"({_fmt_epoch(record.get('imported_at'))})\n    {path}")
    return ("not yet imported -- in the Codex app run "
            "'import external agent session' on:\n"
            f"    {path}")


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _move_codex_to_claude(args: List[str]) -> int:
    """Delegate to the Codex->Claude importer (dry-run unless --write)."""
    from .session_port.cli import main as import_main
    print("move: Codex -> Claude (resumable Claude Code session)\n")
    return import_main(args)


def _resolve_claude_paths(ids: List[str], paths: List[str]) -> List[str]:
    """Local Claude session JSONL paths from explicit --path values and/or
    --id lookups (glob ``~/.claude/projects/**/<id>.jsonl``)."""
    found = list(paths)
    for sid in ids:
        found += glob.glob(str(CLAUDE_PROJECTS / "**" / f"{sid}*.jsonl"), recursive=True)
    # de-dup, keep order
    seen = set()
    return [p for p in found if not (p in seen or seen.add(p))]


def _move_claude_to_codex(opts: dict) -> int:
    """Report Claude->Codex import status (the import itself is an app action)."""
    imports = parse_external_imports(
        EXTERNAL_IMPORTS.read_text() if EXTERNAL_IMPORTS.exists() else "")
    if opts["list"]:
        print(f"Claude sessions already imported into Codex ({len(imports)}):\n")
        if not imports:
            print("  (none)")
        for path, rec in sorted(imports.items(),
                                key=lambda kv: kv[1].get("imported_at", 0), reverse=True):
            print(f"  Codex {rec.get('imported_thread_id', '?')}  "
                  f"{_fmt_epoch(rec.get('imported_at'))}  {path}")
        return 0

    paths = _resolve_claude_paths(opts["ids"], opts["paths"])
    if not paths:
        print("move: Claude -> Codex needs --id <session-id> or --path <file> "
              "(or --list to see what's already imported).", file=sys.stderr)
        return 2
    print("move: Claude -> Codex (app-only import; status below)\n")
    for p in paths:
        print("* " + format_import_status(p, imports.get(p)))
    return 0


USAGE = (
    "usage: python3 -m remote_control work move --from codex|claude [--to ...]\n"
    "  Codex -> Claude (automatable; dry-run unless --write):\n"
    "    --from codex [--id ID]... [--match REGEX] [--limit N] [--list] [--write]\n"
    "      (selectors are passed through to `codex-import`)\n"
    "  Claude -> Codex (app-only import; reports status, no --write):\n"
    "    --from claude [--id SESSION_ID]... [--path FILE]... [--list]\n"
    "      --list shows which Claude sessions Codex has already imported"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"from": None, "to": None, "ids": [], "paths": [], "match": None,
            "limit": None, "list": False, "write": False, "dev": DEV}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--from":
            i += 1; opts["from"] = argv[i]
        elif a == "--to":
            i += 1; opts["to"] = argv[i]
        elif a == "--id":
            i += 1; opts["ids"].append(argv[i])
        elif a == "--path":
            i += 1; opts["paths"].append(argv[i])
        elif a == "--match":
            i += 1; opts["match"] = argv[i]
        elif a == "--limit":
            i += 1; opts["limit"] = argv[i]
        elif a == "--list":
            opts["list"] = True
        elif a == "--write":
            opts["write"] = True
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0
    try:
        from_engine, _to = resolve_direction(opts["from"], opts["to"])
    except ValueError as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2

    if from_engine == "codex":
        passthrough: List[str] = []
        for sid in opts["ids"]:
            passthrough += ["--id", sid]
        if opts["match"]:
            passthrough += ["--match", opts["match"]]
        if opts["limit"]:
            passthrough += ["--limit", opts["limit"]]
        if opts["list"]:
            passthrough.append("--list")
        if opts["write"]:
            passthrough.append("--write")
        return _move_codex_to_claude(passthrough)
    return _move_claude_to_codex(opts)


if __name__ == "__main__":
    raise SystemExit(main())
