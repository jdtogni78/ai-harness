"""Unified, cross-engine inventory of in-flight agent work.

One "what's running where" view spanning **both** engines:

  * **Claude Code** -- live sessions from the code-sessions API (local bridge +
    Anthropic cloud), via :mod:`session_list` / the usage-limit monitor's client.
  * **Codex** -- rollout threads on this machine, via
    :func:`session_port.disk.load_sessions`.

Each side is normalized into one :class:`WorkRow` so they merge into a single
table keyed by engine, repo, host, and a recency-based ``active``/``idle``
activity (the one liveness signal both engines share -- the same 1h TTL the
multi-agent claim convention uses). The ``work stale`` view additionally flags
idle work that looks blocked/waiting (usage-limit paused, disconnected, or
awaiting a response), reusing the usage-limit detectors. These are the
**inventory** and **stale-detect** slices of the cross-engine work-orchestration
epic (trigger / inventory / stale-detect / migrate); the remaining slices build
on knowing current state.

The pure helpers (no network, no clock-of-their-own, no filesystem -- callers
pass ``now`` and inject any timestamp resolution) live above ``main`` so the
classification, merge/filter, and formatting unit-test against plain records.
This is the cross-engine counterpart to the Claude-only :mod:`session_list`.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from .config import DEV, CodexConfig, UsageLimitConfig
from .session_list import build_rows
from .session_titles import build_worktree_index
from .session_port import codex as cx
from .session_port import disk
from .usage_limit import codex as uc
from .usage_limit import detect, monitor

# An agent's work is "active" if it saw activity within this window, else "idle".
# Mirrors the multi-agent claim liveness TTL (AGENT_CLAIM_TTL_SECS, 1h).
DEFAULT_TTL_SECS = 3600
# Codex rollouts are always on the machine that wrote them.
CODEX_LOCATION = "this-host"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``...Z`` or ``+00:00``) to an aware
    datetime, or None if it's empty/unparseable. Naive values are assumed UTC."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def activity_of(last_event_at: str, now: datetime, ttl_secs: int) -> str:
    """``active`` if activity is within *ttl_secs* of *now*, else ``idle``.

    Unknown/unparseable timestamps count as ``idle`` (we can't show that work is
    live, so we don't claim it is)."""
    dt = parse_ts(last_event_at)
    if dt is None:
        return "idle"
    return "active" if 0 <= (now - dt).total_seconds() < ttl_secs else "idle"


def stale_reason(
    *,
    active: bool,
    limit_paused: bool = False,
    disconnected: bool = False,
    awaiting: bool = False,
) -> str:
    """Why an item is *stale* -- looks unfinished but won't progress without
    intervention -- or "" if it isn't.

    Active work is never stale. An idle item is stale only when an *unambiguous*
    blocked/waiting signal is present (highest-priority reason wins); plain
    dormancy (idle with nothing pending) is left unflagged, since "idle" already
    covers "quiet, maybe just finished". We deliberately do NOT flag a "running"
    session whose events went quiet: from the list API a hung worker and one
    mid-long-operation look identical, so that would false-positive on every
    long tool call."""
    if active:
        return ""
    if limit_paused:
        return "usage-limit paused"
    if disconnected:
        return "disconnected (server gone)"
    if awaiting:
        return "awaiting response"
    return ""


def repo_from_cwd(cwd: str, dev_root) -> Optional[str]:
    """Repo basename for a working dir under ``<dev>/<repo>[/...]`` (covers both
    a repo root and a ``<repo>/.claude/worktrees/...`` bridge worktree), else
    None. Purely lexical -- no filesystem access."""
    if not cwd:
        return None
    try:
        rel = Path(cwd).relative_to(Path(dev_root))
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


class WorkRow(NamedTuple):
    engine: str           # "claude" | "codex"
    id: str
    title: str
    repo: Optional[str]
    location: str         # "cloud" | "this-host" | "other-host"
    activity: str         # "active" | "idle" (recency vs TTL)
    detail: str           # engine-native status (may be "")
    last_event_at: str    # ISO timestamp (may be "")
    stale_reason: str = ""  # "" unless idle + blocked/waiting (see stale_reason())

    def as_dict(self) -> dict:
        return self._asdict()


def claude_rows_to_work(
    rows,
    now: datetime,
    ttl_secs: int,
    limit_paused_ids=frozenset(),
) -> List[WorkRow]:
    """Adapt :class:`session_list.Row` records into unified rows.

    *limit_paused_ids* are the session ids the usage-limit detector flagged as
    paused on a usage/session limit (see :func:`usage_limit.detect.limit_pause_detail`)."""
    out: List[WorkRow] = []
    for r in rows:
        activity = activity_of(r.last_event_at, now, ttl_secs)
        out.append(WorkRow(
            engine="claude",
            id=r.id,
            title=r.title,
            repo=r.repo,
            location=r.location,
            activity=activity,
            detail=f"worker={r.worker_status} conn={r.connection_status}",
            last_event_at=r.last_event_at,
            stale_reason=stale_reason(
                active=activity == "active",
                limit_paused=r.id in limit_paused_ids,
                disconnected=r.connection_status == "disconnected",
                awaiting=r.worker_status == "requires_action",
            ),
        ))
    return out


def codex_sessions_to_work(
    sessions,
    dev_root,
    now: datetime,
    ttl_secs: int,
    ts_of: Optional[Callable[["cx.CodexSession"], str]] = None,
    signals_of: Optional[Callable[["cx.CodexSession"], dict]] = None,
) -> List[WorkRow]:
    """Adapt :class:`session_port.codex.CodexSession` records into unified rows.

    *ts_of* resolves a session's effective timestamp (injected so the I/O layer
    can fall back to the rollout file's mtime); it defaults to the index's
    ``updated_at``. *signals_of* (injected I/O) returns a session's stale signals
    (``{limit_paused, awaiting}``); it is consulted only for *idle* sessions
    (active work is never stale) so the per-rollout reads stay off the common
    ``ls`` path."""
    ts_of = ts_of or (lambda s: s.updated_at)
    out: List[WorkRow] = []
    for s in sessions:
        ts = ts_of(s)
        activity = activity_of(ts, now, ttl_secs)
        sig = signals_of(s) if (signals_of and activity != "active") else {}
        out.append(WorkRow(
            engine="codex",
            id=s.id,
            title=s.title,
            repo=repo_from_cwd(s.cwd, dev_root),
            location=CODEX_LOCATION,
            activity=activity,
            detail="archived" if s.archived else "",
            last_event_at=ts,
            stale_reason=stale_reason(
                active=activity == "active",
                limit_paused=sig.get("limit_paused", False),
                awaiting=sig.get("awaiting", False),
            ),
        ))
    return out


def merge_rows(
    rows: List[WorkRow],
    *,
    repo_filter: Optional[str] = None,
    engine_filter: Optional[str] = None,
    include_idle: bool = True,
) -> List[WorkRow]:
    """Filter (by repo / engine / activity) and sort newest activity first."""
    want_repo = repo_filter.lower() if repo_filter else None
    out: List[WorkRow] = []
    for r in rows:
        if engine_filter and r.engine != engine_filter:
            continue
        if want_repo is not None and (r.repo or "").lower() != want_repo:
            continue
        if not include_idle and r.activity != "active":
            continue
        out.append(r)
    out.sort(key=lambda r: parse_ts(r.last_event_at) or _EPOCH, reverse=True)
    return out


def _location_label(location: str, host: str) -> str:
    if location == "this-host":
        return f"this host ({host})"
    if location == "cloud":
        return "cloud sandbox"
    return "other host"


def format_work_rows(rows: List[WorkRow], host: str) -> str:
    """A scannable plaintext block: one stanza per work item."""
    if not rows:
        return "(no work)"
    out: List[str] = []
    for r in rows:
        out.append(f"[{r.engine:<6}] {r.id}")
        out.append(f"    title : {r.title!r}")
        out.append(f"    repo  : {r.repo or '<unknown>'}")
        out.append(f"    where : {_location_label(r.location, host)}  [{r.activity}]")
        if r.detail:
            out.append(f"    state : {r.detail}")
        if r.stale_reason:
            out.append(f"    stale : {r.stale_reason}")
        out.append(f"    last  : {r.last_event_at or '<unknown>'}")
        out.append("")
    return "\n".join(out).rstrip()


def summarize_work(rows: List[WorkRow]) -> str:
    """One-line tallies by engine, repo, and activity."""
    def tally(key) -> str:
        counts: Dict[str, int] = {}
        for r in rows:
            counts[key(r)] = counts.get(key(r), 0) + 1
        return ", ".join(f"{k}×{v}" for k, v in
                         sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    lines = [
        f"by engine  : {tally(lambda r: r.engine)}",
        f"by repo    : {tally(lambda r: r.repo or '<unknown>')}",
        f"by activity: {tally(lambda r: r.activity)}",
    ]
    if any(r.stale_reason for r in rows):
        stale = [r for r in rows if r.stale_reason]
        lines.append(
            "by stale   : "
            + ", ".join(f"{k}×{v}" for k, v in sorted(
                {r.stale_reason: sum(1 for x in stale if x.stale_reason == r.stale_reason)
                 for r in stale}.items(), key=lambda kv: (-kv[1], kv[0]))))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _codex_ts_resolver(now: datetime) -> Callable[["cx.CodexSession"], str]:
    """Effective timestamp for a Codex session: the index ``updated_at`` if
    present, else the rollout file's mtime (so index-less threads still sort and
    classify by real recency)."""
    def ts_of(s: "cx.CodexSession") -> str:
        if s.updated_at:
            return s.updated_at
        try:
            mtime = os.path.getmtime(s.path)
        except OSError:
            return ""
        return datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    return ts_of


def _collect_claude(opts: dict, now: datetime, log) -> Optional[List[WorkRow]]:
    """Fetch + adapt Claude sessions. None on a hard auth/API failure (so the
    caller can warn yet still show Codex)."""
    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain (skipping Claude)")
        return None
    sessions = monitor.list_sessions(cfg, token, log)
    if sessions is None:
        log("could not list Claude sessions (skipping Claude)")
        return None
    index = build_worktree_index(Path(opts["dev"]))
    rows = build_rows(sessions, index, include_archived=opts["all"])
    limit_paused_ids = frozenset(
        s.get("id") for s in sessions if s.get("id") and detect.limit_pause_detail(s))
    return claude_rows_to_work(rows, now, opts["ttl"], limit_paused_ids)


def _codex_signals_resolver() -> Callable[["cx.CodexSession"], dict]:
    """Read an idle Codex rollout for its stale signals: ``limit_paused`` (a
    rate-limit window is exhausted -- :mod:`usage_limit.codex`) and ``awaiting``
    (the last conversation turn is the user's, so the agent never replied)."""
    threshold = CodexConfig.from_env().block_threshold

    def signals_of(s: "cx.CodexSession") -> dict:
        limit_paused = False
        meta = uc.read_rollout(s.path)
        if meta and meta.get("rate_limits"):
            limit_paused = uc.evaluate_block(meta["rate_limits"], threshold)[0]
        awaiting = False
        try:
            with open(s.path) as fh:
                turns = cx.extract_turns(fh.readlines())
            awaiting = bool(turns) and turns[-1][0] == "user"
        except OSError:
            pass
        return {"limit_paused": limit_paused, "awaiting": awaiting}
    return signals_of


def _collect_codex(opts: dict, now: datetime, want_signals: bool = False) -> List[WorkRow]:
    sessions = disk.load_sessions(opts["all"])
    return codex_sessions_to_work(
        sessions, opts["dev"], now, opts["ttl"], ts_of=_codex_ts_resolver(now),
        signals_of=_codex_signals_resolver() if want_signals else None)


USAGE = (
    "usage: python3 -m remote_control work [ls|stale|move|start] "
    "[--all] [--repo REPO] [--engine claude|codex] [--ttl SECS] [--json] [--dev DIR]\n"
    "  ls (default): in-flight work across Claude Code + Codex, with repo/host/activity\n"
    "  stale       : only idle work that looks blocked/waiting (usage-limit paused,\n"
    "                disconnected, or awaiting response) -- with the reason\n"
    "  move        : move work between engines (see `work move --help`)\n"
    "  start       : trigger fresh agent work from a prompt (see `work start --help`)\n"
    "  --all    : include idle (older than --ttl) and archived work too (ls only)\n"
    "  --repo   : only this repo basename (case-insensitive)\n"
    "  --engine : restrict to one engine (claude or codex)\n"
    "  --ttl    : seconds of inactivity before work counts as idle "
    f"(default {DEFAULT_TTL_SECS}; env REMOTE_CONTROL_WORK_TTL_SECS)\n"
    "  --json   : machine-readable rows instead of the table\n"
    "  --dev    : dev root for repo lookup (default ~/dev)"
)


def _parse_args(argv: List[str]) -> dict:
    default_ttl = int(os.environ.get("REMOTE_CONTROL_WORK_TTL_SECS", DEFAULT_TTL_SECS))
    opts = {"cmd": "ls", "all": False, "repo": None, "engine": None, "ttl": default_ttl,
            "json": False, "dev": DEV}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("ls", "stale"):
            opts["cmd"] = a
        elif a == "--all":
            opts["all"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--repo":
            i += 1; opts["repo"] = argv[i]
        elif a == "--engine":
            i += 1; opts["engine"] = argv[i]
        elif a == "--ttl":
            i += 1; opts["ttl"] = int(argv[i])
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    if opts["engine"] not in (None, "claude", "codex"):
        raise ValueError(f"--engine must be claude or codex, got {opts['engine']!r}")
    return opts


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `work move` / `work start` are actions, not read-only views -- hand each to
    # its own module (separate flags) before the ls/stale parser sees them.
    if argv and argv[0] == "move":
        from .work_move import main as move_main
        return move_main(argv[1:])
    if argv and argv[0] == "start":
        from .work_start import main as start_main
        return start_main(argv[1:])
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 (API client diagnostics)
    now = datetime.now(timezone.utc)
    is_stale = opts["cmd"] == "stale"
    want_claude = opts["engine"] in (None, "claude")
    want_codex = opts["engine"] in (None, "codex")

    work: List[WorkRow] = []
    claude_failed = False
    if want_claude:
        claude = _collect_claude(opts, now, log)
        if claude is None:
            claude_failed = True
        else:
            work += claude
    if want_codex:
        work += _collect_codex(opts, now, want_signals=is_stale)

    # Stale items are idle by definition, so the stale view must look past the
    # in-flight filter; then it keeps only the items with a stale reason.
    rows = merge_rows(work, repo_filter=opts["repo"], engine_filter=opts["engine"],
                      include_idle=opts["all"] or is_stale)
    if is_stale:
        rows = [r for r in rows if r.stale_reason]

    if opts["json"]:
        print(json.dumps([r.as_dict() for r in rows], indent=2))
        return 1 if claude_failed else 0

    host = socket.gethostname()
    scope = ("stale work items" if is_stale
             else "work items" if opts["all"] else "in-flight work items")
    print(f"host={host}  {len(rows)} {scope}\n")
    print(format_work_rows(rows, host))
    if rows:
        print(f"\n{summarize_work(rows)}")
    if claude_failed:
        print("\n(note: Claude side unavailable — showing Codex only)", file=sys.stderr)
    return 1 if claude_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
