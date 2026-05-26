"""Fork a Claude Code session into a *local sibling*: a new session id that
carries the old one's full history, with the original transcript left untouched.

Why this exists: ``claude -r <id> --fork-session -p`` does NOT fork in print
mode -- it appends the new turn to the *original* transcript and keeps the same
session id. The reliable fork is mechanical: copy the session's ``.jsonl`` to a
fresh ``<uuid>.jsonl``, rewriting the embedded ``sessionId`` (and, when the
sibling moves to a different working dir, the ``cwd``) so the copy is a coherent,
independently-resumable session.

Sessions live on disk at ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``,
where ``<encoded-cwd>`` is the absolute cwd with every non-alphanumeric character
replaced by ``-`` (so ``/Users/me/dev/job-search`` ->
``-Users-me-dev-job-search``). ``claude -r <id>`` finds a session by that on-disk
layout, so a fork is "write ``<newid>.jsonl`` into the project dir for the cwd we
want to resume from".

``--into-main`` ("mirror the main directory") places the sibling in the repo's
*main* checkout (``<dev>/<repo>``) instead of the bridge worktree the source ran
in, rewriting ``cwd`` to match -- so the resumed sibling operates on the primary
clone. ``--mark`` retitles the *source* cloud (``cse_``) session to point at the
fork (reusing the title writer in :mod:`session_titles`); it's the only step that
touches the network, and it never wakes the source agent.

The pure helpers (no network, clock, or filesystem) live above ``main`` so the
path encoding, id extraction, and transcript rewrite unit-test against strings.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional

from .config import DEV, UsageLimitConfig
from .session_titles import (
    NICKNAMES_FILE,
    apply_prefix,
    build_nickname_map,
    build_worktree_index,
    nickname_for,
    set_title,
    strip_prefix,
)
from .usage_limit import monitor

# A `cse_` bridge session's id, as it survives the project-dir encoding: the
# worktree basename `bridge-cse_<tail>` becomes `bridge-cse-<tail>`.
_CSE_IN_DIRNAME = re.compile(r"bridge-cse-([0-9A-Za-z]+)")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def encode_project_dir(cwd: str) -> str:
    """Absolute cwd -> the ``~/.claude/projects`` subdir name Claude Code uses:
    every non-alphanumeric char becomes ``-`` (one dash per char, not collapsed,
    so ``/.claude`` -> ``--claude``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def cse_id_from_project_dirname(name: str) -> Optional[str]:
    """The ``cse_`` id owning a project dir name (its trailing
    ``...bridge-cse-<tail>``), or None if it isn't a bridge worktree's dir."""
    matches = _CSE_IN_DIRNAME.findall(name)
    return f"cse_{matches[-1]}" if matches else None


def first_cwd(lines: Iterable[str]) -> Optional[str]:
    """The first ``cwd`` recorded in a transcript (its originating working dir),
    or None. Used to know where ``claude -r`` must run from for a same-dir fork."""
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip():
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get("cwd"):
            return obj["cwd"]
    return None


class Rewrite(NamedTuple):
    lines: List[str]
    sid_count: int  # lines whose sessionId was rewritten
    cwd_count: int  # lines whose cwd was rewritten


def rewrite_transcript_lines(
    lines: Iterable[str], new_sid: str, new_cwd: Optional[str] = None
) -> Rewrite:
    """Rewrite ``sessionId`` -> *new_sid* on every line that has one, and (only
    when *new_cwd* is given) ``cwd`` -> *new_cwd* likewise. Blank lines and lines
    that aren't a JSON object pass through verbatim, so an unparseable transcript
    is copied rather than corrupted."""
    out: List[str] = []
    sid_count = cwd_count = 0
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip():
            out.append(s)
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            out.append(s)
            continue
        if isinstance(obj, dict):
            if "sessionId" in obj:
                obj["sessionId"] = new_sid
                sid_count += 1
            if new_cwd is not None and "cwd" in obj:
                obj["cwd"] = new_cwd
                cwd_count += 1
            out.append(json.dumps(obj))
        else:
            out.append(s)
    return Rewrite(out, sid_count, cwd_count)


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def default_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_source_jsonl(session: str, projects_root: Path) -> Path:
    """The newest transcript file for *session* (a ``cse_`` id or a local uuid).

    A ``cse_`` id maps to its bridge worktree's project dir(s); a uuid is the
    transcript filename directly. Raises ``FileNotFoundError`` if none match."""
    root = Path(projects_root)
    cands: List[Path] = []
    if session.startswith("cse_"):
        for d in root.glob(f"*bridge-cse-{session[len('cse_'):]}"):
            cands += d.glob("*.jsonl")
    else:
        cands += root.glob(f"*/{session}.jsonl")
    cands = [c for c in cands if c.is_file()]
    if not cands:
        raise FileNotFoundError(f"no local transcript for session {session!r}")
    return max(cands, key=lambda c: c.stat().st_mtime)


def repo_for_cse(cse_id: Optional[str], dev: str) -> Optional[str]:
    """The repo a bridge session belongs to, via the local worktree index."""
    if not cse_id:
        return None
    return build_worktree_index(Path(dev)).get(cse_id)


class ForkResult(NamedTuple):
    new_sid: str
    src: Path
    dest: Path
    run_dir: str          # cwd to resume the sibling from
    relocated: bool       # True if the sibling moved to a different cwd
    repo: Optional[str]
    cse_source: Optional[str]
    sid_count: int
    cwd_count: int
    line_count: int
    written: bool


def do_fork(
    session: str,
    *,
    projects_root: Path,
    dev: str,
    new_sid: Optional[str] = None,
    into_main: bool = False,
    dest_dir: Optional[str] = None,
    write: bool = True,
) -> ForkResult:
    """Fork *session* into a new local session id (the heart of the command).

    Destination cwd precedence: explicit *dest_dir* > *into_main* (the repo's
    ``<dev>/<repo>`` main checkout) > the source's own cwd (a same-dir sibling,
    cwd left as-is). When the cwd changes the copied ``cwd`` fields are rewritten
    to match. With *write* False nothing is written (dry run); counts are still
    computed."""
    root = Path(projects_root)
    src = find_source_jsonl(session, root)
    new_sid = new_sid or str(uuid.uuid4())
    raw = src.read_text().splitlines()

    cse_source = (
        session if session.startswith("cse_")
        else cse_id_from_project_dirname(src.parent.name)
    )
    repo = repo_for_cse(cse_source, dev)

    if dest_dir:
        run_dir, relocated = str(dest_dir), True
    elif into_main:
        if not repo:
            raise ValueError(
                f"--into-main: can't resolve a repo for {session!r}; "
                f"pass --dest-dir explicitly"
            )
        run_dir, relocated = str(Path(dev) / repo), True
    else:
        run_dir = first_cwd(raw) or str(src.parent)
        relocated = False

    rw = rewrite_transcript_lines(raw, new_sid, run_dir if relocated else None)
    dest_parent = root / encode_project_dir(run_dir) if relocated else src.parent
    dest = dest_parent / f"{new_sid}.jsonl"
    if write:
        dest_parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(rw.lines) + "\n")

    return ForkResult(
        new_sid=new_sid, src=src, dest=dest, run_dir=run_dir, relocated=relocated,
        repo=repo, cse_source=cse_source, sid_count=rw.sid_count,
        cwd_count=rw.cwd_count, line_count=len(raw), written=write,
    )


def mark_source_moved(
    cfg: UsageLimitConfig, token: str, res: ForkResult, dev: str,
    text: Optional[str], log,
) -> int:
    """Retitle the source cloud session to point at the fork (idempotent prefix
    handling via :mod:`session_titles`). No-op-with-warning if the source has no
    ``cse_`` id (a purely local session has no cloud title to set)."""
    if not res.cse_source:
        log("--mark: source has no cloud (cse_) session to retitle; skipping")
        return 1
    sessions = monitor.list_sessions(cfg, token, log) or []
    cur = next((s for s in sessions if s.get("id") == res.cse_source), None)
    base = strip_prefix((cur or {}).get("title") or "")
    marker = text or f"→ forked to {res.new_sid[:8]}"
    desc = f"{base} {marker}".strip()
    try:
        nmap = build_nickname_map(Path(NICKNAMES_FILE).read_text(),
                                  os.environ.get("SESSION_TITLE_NICKNAMES", ""))
    except OSError:
        nmap = build_nickname_map("", os.environ.get("SESSION_TITLE_NICKNAMES", ""))
    title = apply_prefix(desc, nickname_for(res.repo, nmap)) if res.repo else desc
    code, body = set_title(cfg, token, res.cse_source, title)
    if code == 200:
        print(f"marked {res.cse_source} -> {title!r}")
        return 0
    log(f"--mark FAILED {res.cse_source} http={code} body={str(body)[:160]}")
    return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control fork <SESSION> [--into-main | --dest-dir DIR]\n"
    "         [--mark [--mark-text TEXT]] [--run \"PROMPT\" [--model M]]\n"
    "         [--new-id UUID] [--dev DIR] [--projects DIR] [--dry-run]\n"
    "  SESSION      : a cse_ id or a local session uuid to fork\n"
    "  --into-main  : put the sibling in the repo's main checkout <dev>/<repo>\n"
    "                 (mirror the main directory), rewriting cwd to match\n"
    "  --dest-dir   : put the sibling in this cwd instead (overrides --into-main)\n"
    "  --mark       : retitle the source cloud session to point at the fork\n"
    "  --run        : after forking, run one turn in the sibling (PROMPT)\n"
    "  --dry-run    : resolve + report only; write nothing, call no API"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {
        "session": None, "into_main": False, "dest_dir": None, "mark": False,
        "mark_text": None, "run": None, "model": None, "new_id": None,
        "dev": DEV, "projects": None, "dry_run": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--into-main":
            opts["into_main"] = True
        elif a == "--dest-dir":
            i += 1; opts["dest_dir"] = argv[i]
        elif a == "--mark":
            opts["mark"] = True
        elif a == "--mark-text":
            i += 1; opts["mark_text"] = argv[i]
        elif a == "--run":
            i += 1; opts["run"] = argv[i]
        elif a == "--model":
            i += 1; opts["model"] = argv[i]
        elif a == "--new-id":
            i += 1; opts["new_id"] = argv[i]
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a == "--projects":
            i += 1; opts["projects"] = argv[i]
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif not a.startswith("-") and opts["session"] is None:
            opts["session"] = a
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def _report(res: ForkResult, dry: bool) -> None:
    verb = "would fork" if dry else "forked"
    print(f"{verb} {res.cse_source or res.src.name} -> new session {res.new_sid}")
    print(f"  repo       : {res.repo or '<unknown>'}")
    print(f"  source     : {res.src}")
    print(f"  sibling    : {res.dest}{'' if res.written else '  (not written: dry run)'}")
    where = "main checkout" if res.relocated else "same dir as source"
    print(f"  run dir    : {res.run_dir}  ({where})")
    print(f"  rewrote    : sessionId x{res.sid_count}, cwd x{res.cwd_count} "
          f"of {res.line_count} lines")
    print(f"\n  resume it  : (cd {res.run_dir!r} && claude -r {res.new_sid})")


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
    if not opts["session"]:
        print(f"fork requires a SESSION (cse_ id or uuid)\n{USAGE}", file=sys.stderr)
        return 2

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 (diagnostics)
    projects = Path(opts["projects"]) if opts["projects"] else default_projects_root()
    try:
        res = do_fork(
            opts["session"], projects_root=projects, dev=opts["dev"],
            new_sid=opts["new_id"], into_main=opts["into_main"],
            dest_dir=opts["dest_dir"], write=not opts["dry_run"],
        )
    except (FileNotFoundError, ValueError) as e:
        log(str(e))
        return 1

    _report(res, opts["dry_run"])

    rc = 0
    if opts["mark"] and not opts["dry_run"]:
        cfg = UsageLimitConfig.from_env()
        token = monitor.get_token(cfg, log)
        if not token:
            log("could not read OAuth token from keychain; skipping --mark")
            rc = 1
        else:
            rc = mark_source_moved(cfg, token, res, opts["dev"],
                                   opts["mark_text"], log) or rc

    if opts["run"] and not opts["dry_run"]:
        cmd = ["claude", "-r", res.new_sid]
        if opts["model"]:
            cmd += ["--model", opts["model"]]
        cmd += ["-p", opts["run"]]
        print(f"\n  running    : {' '.join(cmd)}  (cwd {res.run_dir})")
        rc = subprocess.run(cmd, cwd=res.run_dir).returncode or rc

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
