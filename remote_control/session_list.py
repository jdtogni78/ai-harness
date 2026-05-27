"""List the *active* (non-archived) Claude Code sessions, annotated with the repo
they belong to and the host/sandbox they run on.

The code-sessions API (``GET /v1/code/sessions``) returns both archived and live
sessions mixed together; this surfaces just the live ones in a scannable table so
you can see, at a glance, what's running where across repos and machines. It's the
read-only inventory counterpart to the title-rewriting in :mod:`session_titles`
(whose pure repo-derivation + worktree-index helpers it reuses) and feeds the
``resume-work`` skill's "what's live right now" step.

Host / sandbox classification (``classify_location``):
  * ``cloud``      -- ``environment_kind == "anthropic_cloud"`` (an Anthropic
                      cloud sandbox; not tied to any local machine).
  * ``this-host``  -- a bridge session whose ``cse_`` id matches a local bridge
                      worktree under ``~/dev`` (so it runs on *this* Mac).
  * ``other-host`` -- a bridge session with no local worktree: it runs on another
                      machine (the API can't tell us which), so we can only say
                      "not here".

The pure helpers (no network, clock, or filesystem) live above ``main`` so the
filtering, classification, and table formatting unit-test against plain dicts.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from .config import DEV, UsageLimitConfig
from .session_titles import build_worktree_index, parse_cmd_session_id, repo_for_session
from .usage_limit import monitor

ARCHIVED_STATUS = "archived"
CLOUD_KIND = "anthropic_cloud"
DEFAULT_STALE_AGE_SECS = 3600  # 1h: matches AGENT_CLAIM_TTL_SECS in CLAUDE.md


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def is_active(session: dict) -> bool:
    """A session is active (non-archived) unless its ``status`` is ``archived``.

    Defined as "not archived" rather than "status == active" so any future
    non-archived status (e.g. a paused/limited state) still shows up."""
    return (session.get("status") or "") != ARCHIVED_STATUS


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNIT_SECS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """``"1h"`` -> 3600. Bare digits are seconds. Raises ``ValueError`` on garbage.

    Supports the four units anyone reaches for: s / m / h / d. Kept deliberately
    permissive (whitespace, missing unit) since this comes off a CLI flag."""
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(f"bad duration: {text!r}")
    return int(m.group(1)) * _UNIT_SECS[m.group(2)]


def _parse_event_epoch(ts: str) -> Optional[float]:
    """ISO-8601 ``last_event_at`` -> epoch seconds, or None if unparseable.
    Tolerates the trailing ``Z`` form the API emits."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def is_stale(session: dict, now: float, older_than_secs: int,
             require_disconnected: bool = False) -> bool:
    """A session is "stale" iff worker_status=idle AND its last_event_at is
    older than *older_than_secs* (and, when *require_disconnected*, also
    disconnected). Running sessions are never stale -- we only fork things the
    user has stopped touching."""
    if (session.get("worker_status") or "") != "idle":
        return False
    if require_disconnected and (session.get("connection_status") or "") != "disconnected":
        return False
    last = _parse_event_epoch(session.get("last_event_at") or "")
    if last is None:
        return False
    return (now - last) >= older_than_secs


def classify_location(session: dict, worktree_index: Dict[str, str]) -> str:
    """Where the session runs: ``cloud`` / ``this-host`` / ``other-host``.

    *worktree_index* maps ``cse_`` id -> repo for the bridge worktrees that exist
    on *this* machine (see :func:`session_titles.build_worktree_index`), so
    membership in it is the signal that a bridge session is local."""
    if session.get("environment_kind") == CLOUD_KIND:
        return "cloud"
    return "this-host" if session.get("id") in worktree_index else "other-host"


class Row(NamedTuple):
    id: str
    title: str
    repo: Optional[str]
    env_kind: str
    location: str
    worker_status: str
    connection_status: str
    last_event_at: str

    def as_dict(self) -> dict:
        return self._asdict()


def build_rows(
    sessions: List[dict],
    worktree_index: Dict[str, str],
    *,
    include_archived: bool = False,
    repo_filter: Optional[str] = None,
    stale_only: bool = False,
    stale_age_secs: int = DEFAULT_STALE_AGE_SECS,
    stale_require_disconnected: bool = False,
    now: Optional[float] = None,
    location_filter: Optional[str] = None,
) -> List[Row]:
    """One :class:`Row` per session, newest activity first.

    Filters to active sessions unless *include_archived*; *repo_filter* (a repo
    basename, case-insensitive) narrows to a single repo; *stale_only* drops
    everything that isn't stale (see :func:`is_stale`); *location_filter*
    (``this-host`` / ``other-host`` / ``cloud``) narrows by where it runs."""
    import time
    want_repo = repo_filter.lower() if repo_filter else None
    now_t = time.time() if now is None else now
    rows: List[Row] = []
    for s in sessions:
        if not include_archived and not is_active(s):
            continue
        repo = repo_for_session(s, worktree_index)
        if want_repo is not None and (repo or "").lower() != want_repo:
            continue
        if stale_only and not is_stale(s, now_t, stale_age_secs,
                                       stale_require_disconnected):
            continue
        loc = classify_location(s, worktree_index)
        if location_filter is not None and loc != location_filter:
            continue
        rows.append(Row(
            id=s.get("id") or "",
            title=s.get("title") or "",
            repo=repo,
            env_kind=s.get("environment_kind") or "?",
            location=loc,
            worker_status=s.get("worker_status") or "?",
            connection_status=s.get("connection_status") or "?",
            last_event_at=s.get("last_event_at") or "",
        ))
    rows.sort(key=lambda r: r.last_event_at, reverse=True)
    return rows


def _location_label(location: str, host: str) -> str:
    """Human label for a row's location, naming this machine when local."""
    if location == "this-host":
        return f"this host ({host})"
    if location == "cloud":
        return "cloud sandbox"
    return "other host"


def format_rows(rows: List[Row], host: str) -> str:
    """A scannable plaintext block: one stanza per session."""
    if not rows:
        return "(no sessions)"
    out: List[str] = []
    for r in rows:
        out.append(r.id)
        out.append(f"    title : {r.title!r}")
        out.append(f"    repo  : {r.repo or '<unknown>'}")
        out.append(f"    host  : {r.env_kind} -> {_location_label(r.location, host)}")
        out.append(f"    state : worker={r.worker_status}  conn={r.connection_status}")
        out.append(f"    last  : {r.last_event_at}")
        out.append("")
    return "\n".join(out).rstrip()


def summarize(rows: List[Row]) -> str:
    """One-line tallies by repo and by location."""
    def tally(key) -> str:
        counts: Dict[str, int] = {}
        for r in rows:
            counts[key(r)] = counts.get(key(r), 0) + 1
        return ", ".join(f"{k}×{v}" for k, v in
                         sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"by repo: {tally(lambda r: r.repo or '<unknown>')}\n"
            f"by host: {tally(lambda r: r.location)}")


def own_session_id_from_env(env: Dict[str, str]) -> Optional[str]:
    """The ``cse_*`` id of the session this process runs inside, decoded from
    the ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` env var the desktop app exposes
    on every spawned process (the JWT's ``session_id`` claim). ``None`` if
    the env var is absent or unparseable -- in which case the caller hasn't
    been launched inside a Claude Code session, e.g. a manual shell run."""
    token = env.get("CLAUDE_CODE_SESSION_ACCESS_TOKEN") or ""
    return parse_cmd_session_id(token) if token else None


REPLY_HEADER_FMT = "[from {sid} — reply via send-to-session]\n\n"


def format_reply_header(sender_sid: str, message: str) -> str:
    """Prepend a one-line sender-id header so the receiving agent knows which
    ``cse_*`` to reply back into. The em dash + bracket form is hard to confuse
    with normal prose and easy to match with a regex on the receiver side."""
    return REPLY_HEADER_FMT.format(sid=sender_sid) + message


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control sessions [list] "
    "[--all] [--repo REPO] [--json | --ids-only] [--dev DIR]\n"
    "         [--stale [--older-than DUR] [--disconnected]] [--location LOC]\n"
    "       python3 -m remote_control sessions archive CSE_ID [CSE_ID...] [--dry-run]\n"
    "       python3 -m remote_control sessions submit CSE_ID (--message TEXT | --stdin)\n"
    "                                                  [--reply-to CSE_ID | --no-reply-to] [--dry-run]\n"
    "  list (default): active (non-archived) sessions, with repo + host/sandbox\n"
    "  --all          : include archived sessions too\n"
    "  --repo         : only sessions for this repo basename (case-insensitive)\n"
    "  --json         : machine-readable rows instead of the table\n"
    "  --ids-only     : just the cse_ ids, one per line (pipe to fork-all --ids)\n"
    "  --stale        : only idle sessions older than --older-than (default 1h)\n"
    "  --older-than   : age threshold, e.g. 30m, 2h, 1d (default 1h)\n"
    "  --disconnected : with --stale, also require connection_status=disconnected\n"
    "  --location     : narrow to this-host / other-host / cloud\n"
    "  --dev          : dev root for bridge-worktree repo lookup (default ~/dev)\n"
    "  archive: POST /sessions/{id}/archive for each id; one-line status per id;\n"
    "           exit 0 only if every archive returned 200 (--dry-run prints only)\n"
    "  submit : POST a user-turn message into CSE_ID; --stdin reads the message\n"
    "           from stdin (use for multi-line); --dry-run prints the payload.\n"
    "           Prepends a `[from <sender-cse_id> — reply via send-to-session]`\n"
    "           header so the receiving agent knows where to reply; the sender id\n"
    "           is auto-detected from CLAUDE_CODE_SESSION_ACCESS_TOKEN. Override\n"
    "           with --reply-to CSE_ID, or skip the header entirely with --no-reply-to."
)


def _parse_args(argv: List[str]) -> dict:
    opts = {
        "all": False, "repo": None, "json": False, "ids_only": False,
        "dev": DEV, "stale": False, "older_than": DEFAULT_STALE_AGE_SECS,
        "disconnected": False, "location": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "list":
            pass
        elif a == "--all":
            opts["all"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--ids-only":
            opts["ids_only"] = True
        elif a == "--stale":
            opts["stale"] = True
        elif a == "--disconnected":
            opts["disconnected"] = True
        elif a == "--older-than":
            i += 1; opts["older_than"] = parse_duration(argv[i])
        elif a == "--location":
            i += 1; loc = argv[i]
            if loc not in ("this-host", "other-host", "cloud"):
                raise ValueError(f"--location must be this-host|other-host|cloud, got {loc!r}")
            opts["location"] = loc
        elif a == "--repo":
            i += 1; opts["repo"] = argv[i]
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    if opts["json"] and opts["ids_only"]:
        raise ValueError("--json and --ids-only are mutually exclusive")
    return opts


def _run_archive(argv: List[str], log) -> int:
    """``sessions archive <id>...`` — POST /sessions/{id}/archive per id."""
    dry = False
    ids: List[str] = []
    for a in argv:
        if a == "--dry-run":
            dry = True
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        elif a.startswith("-"):
            print(f"unknown arg: {a}\n{USAGE}", file=sys.stderr)
            return 2
        else:
            ids.append(a)
    if not ids:
        print(f"archive requires at least one CSE_ID\n{USAGE}", file=sys.stderr)
        return 2
    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    all_ok = True
    for sid in ids:
        if dry:
            print(f"would archive {sid}")
            continue
        code, _ = monitor.archive_session(cfg, token, sid, log)
        if code == 200:
            print(f"archived {sid}")
        else:
            print(f"FAILED {sid} (http={code})")
            all_ok = False
    return 0 if all_ok else 1


def _run_submit(argv: List[str], log) -> int:
    """``sessions submit <id> --message TEXT | --stdin`` — POST a user turn.

    The body is the same wrapped-event shape the usage-limit monitor uses to
    nudge a paused session (see :func:`usage_limit.detect.resume_event_body`).
    Side-effecting on a live session, so it requires one of ``--message`` /
    ``--stdin`` (no defaults) and rejects an empty message.

    By default the message is prefixed with a ``[from <sender-cse_id> — reply
    via send-to-session]`` line so the receiver can reply back; the sender id
    is auto-detected from ``CLAUDE_CODE_SESSION_ACCESS_TOKEN``. ``--reply-to
    CSE_ID`` overrides; ``--no-reply-to`` skips the header (e.g. for nudging
    your own paused session)."""
    sid: Optional[str] = None
    message: Optional[str] = None
    read_stdin = False
    dry = False
    reply_to: Optional[str] = None
    no_reply_to = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return 0
        if a == "--dry-run":
            dry = True
        elif a == "--stdin":
            read_stdin = True
        elif a == "--message":
            i += 1
            if i >= len(argv):
                print("--message requires an argument\n" + USAGE, file=sys.stderr)
                return 2
            message = argv[i]
        elif a == "--reply-to":
            i += 1
            if i >= len(argv):
                print("--reply-to requires a CSE_ID\n" + USAGE, file=sys.stderr)
                return 2
            reply_to = argv[i]
        elif a == "--no-reply-to":
            no_reply_to = True
        elif a.startswith("-"):
            print(f"unknown arg: {a}\n{USAGE}", file=sys.stderr)
            return 2
        elif sid is None:
            sid = a
        else:
            print(f"unexpected positional: {a}\n{USAGE}", file=sys.stderr)
            return 2
        i += 1
    if not sid:
        print(f"submit requires a CSE_ID\n{USAGE}", file=sys.stderr)
        return 2
    if read_stdin and message is not None:
        print("--message and --stdin are mutually exclusive\n" + USAGE, file=sys.stderr)
        return 2
    if reply_to and no_reply_to:
        print("--reply-to and --no-reply-to are mutually exclusive\n" + USAGE, file=sys.stderr)
        return 2
    if read_stdin:
        message = sys.stdin.read()
    if message is None:
        print("submit requires --message TEXT or --stdin\n" + USAGE, file=sys.stderr)
        return 2
    if not message.strip():
        print("submit: message is empty", file=sys.stderr)
        return 2

    if not no_reply_to:
        sender = reply_to or own_session_id_from_env(os.environ)
        if sender:
            message = format_reply_header(sender, message)
            log(f"embedded reply-to header: from {sender}")
        elif not reply_to:
            log("no sender cse_id detected (CLAUDE_CODE_SESSION_ACCESS_TOKEN unset); "
                "receiver won't know where to reply — pass --reply-to CSE_ID or "
                "--no-reply-to to silence this")

    cfg = UsageLimitConfig.from_env()
    if dry:
        from .usage_limit.detect import resume_event_body
        print(f"would POST {cfg.api_base}/sessions/{sid}/events")
        print(json.dumps(resume_event_body(message), indent=2))
        return 0
    token = monitor.get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    code, body = monitor.submit_user_message(cfg, token, sid, message, log)
    if code == 200:
        print(f"submitted {sid} ({len(message)} chars)")
        return 0
    print(f"FAILED {sid} (http={code}) body={str(body)[:200]}", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 (API client diagnostics)
    if argv and argv[0] == "archive":
        return _run_archive(argv[1:], log)
    if argv and argv[0] == "submit":
        return _run_submit(argv[1:], log)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0

    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    sessions = monitor.list_sessions(cfg, token, log)
    if sessions is None:
        return 1

    index = build_worktree_index(Path(opts["dev"]))
    rows = build_rows(
        sessions, index,
        include_archived=opts["all"], repo_filter=opts["repo"],
        stale_only=opts["stale"], stale_age_secs=opts["older_than"],
        stale_require_disconnected=opts["disconnected"],
        location_filter=opts["location"],
    )
    host = socket.gethostname()

    if opts["ids_only"]:
        for r in rows:
            print(r.id)
        return 0
    if opts["json"]:
        print(json.dumps([r.as_dict() for r in rows], indent=2))
        return 0

    scope = "sessions" if opts["all"] else "active (non-archived) sessions"
    if opts["stale"]:
        scope = (f"stale {scope} (idle >= {opts['older_than']}s"
                 + (", disconnected" if opts["disconnected"] else "") + ")")
    if opts["location"]:
        scope = f"{opts['location']} {scope}"
    print(f"host={host}  {len(rows)} {scope} (of {len(sessions)} fetched)\n")
    print(format_rows(rows, host))
    if rows:
        print(f"\n{summarize(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
