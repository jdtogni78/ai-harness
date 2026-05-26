"""``python3 -m remote_control codex-import`` -- convert Codex rollout sessions
into resumable Claude Code sessions.

Codex stores one rollout JSONL per thread; Claude Code stores one session JSONL
per conversation under ``~/.claude/projects/<encoded-cwd>/``. This reads the
former and writes the latter as clean user/agent text turns (reasoning + tool
calls are dropped so resume stays valid). Dry-run by default; pass ``--write``
to create files.

The reverse direction (Claude -> Codex) is already a built-in Codex feature
("import external agent session", recorded in
``~/.codex/external_agent_session_imports.json``), so it is intentionally not
duplicated here.

The rollout-dir I/O lives in :mod:`.disk` (shared with the inventory); the
parsing (:mod:`.codex`) and building (:mod:`.claude`) this calls are pure.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from . import claude as cl
from . import codex as cx
from .disk import load_sessions as _load_sessions

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def _convert_one(sess: cx.CodexSession, dest: Optional[str], write: bool):
    with open(sess.path) as fh:
        lines = fh.readlines()
    turns = cl.merge_turns(cx.extract_turns(lines))
    title = sess.title or (turns[0][1][:60] if turns else "(untitled)")
    cwd = sess.cwd or str(Path.home())
    sid, records = cl.build_session(title, turns, cwd)
    dest_dir = Path(dest) if dest else CLAUDE_PROJECTS / cl.encode_project_dir(cwd)
    out_path = dest_dir / (sid + ".jsonl")
    if write:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
    n_user = sum(1 for t in turns if t[0] == "user")
    n_asst = sum(1 for t in turns if t[0] == "assistant")
    return sid, out_path, n_user, n_asst


def _print_list(sessions: List[cx.CodexSession]) -> None:
    home = str(Path.home())
    print("%-3s %-42s %-26s %s" % ("#", "title", "cwd", "id"))
    for i, s in enumerate(sessions, 1):
        cwd = (s.cwd or "?").replace(home, "~")
        flag = "  [archived]" if s.archived else ""
        print("%-3d %-42s %-26s %s%s" % (
            i, (s.title or "(untitled)")[:42], cwd[:26], s.id, flag))
    print("\n%d session(s). Select with --id/--match/--cwd/--limit, then --write."
          % len(sessions))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m remote_control codex-import",
        description="Convert Codex rollout sessions into resumable Claude Code sessions.",
    )
    ap.add_argument("--list", action="store_true",
                    help="list Codex sessions (with the same filters) and exit")
    ap.add_argument("--id", action="append", dest="ids", metavar="ID",
                    help="select a Codex thread id (a prefix is enough); repeatable")
    ap.add_argument("--match", metavar="REGEX",
                    help="select by thread title (case-insensitive regex)")
    ap.add_argument("--cwd", metavar="PATH",
                    help="only sessions whose working dir == PATH")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="keep only the N most-recently-updated matches")
    ap.add_argument("--include-archived", action="store_true",
                    help="also scan ~/.codex/archived_sessions")
    ap.add_argument("--dest", metavar="DIR",
                    help="write into this dir instead of deriving it from each session's cwd")
    ap.add_argument("--write", action="store_true",
                    help="actually create files (default: dry-run)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    sessions = _load_sessions(args.include_archived)
    has_filter = bool(args.ids or args.match or args.cwd or args.limit)
    chosen = cx.select(sessions, ids=args.ids, match=args.match,
                       cwd=args.cwd, limit=args.limit)

    if args.list:
        _print_list(chosen)
        return 0

    if not has_filter:
        print("refusing to convert every session. First inspect with --list, then\n"
              "narrow with --id / --match / --cwd / --limit (e.g. --limit 5).",
              flush=True)
        return 2
    if not chosen:
        print("no sessions matched; run --list to see what's available.")
        return 1

    print("MODE:", "WRITE" if args.write else "DRY-RUN (pass --write to create files)")
    for s in chosen:
        sid, out_path, nu, na = _convert_one(s, args.dest, args.write)
        print("* %-42s u=%-3d a=%-3d" % ((s.title or "(untitled)")[:42], nu, na))
        print("    %s  ->  %s" % (s.id, out_path))
    suffix = "" if args.write else " (dry-run; nothing written)"
    print("done. %d session(s)%s." % (len(chosen), suffix))
    return 0
