"""Reap leaked one-shot ``claude remote-control`` servers.

A ``new-session`` one-shot is spawned with ``--capacity 1`` on the documented
belief that the server exits when its single session ends. It does not:
``--capacity`` caps *concurrency*, not process lifetime. When the inner session
ends -- cleanly or by crashing -- the server prints ``Ready · Capacity: 0/1``
and idles forever waiting for a session that will never come. So every one-shot
ever spawned leaks a process. On 2026-07-21 that had accumulated to 155 dead
servers on one host, burying the real dev server in the app's computer list.

This module is the durable fix: a sweep the supervisor runs each tick that
SIGTERMs a one-shot only when **two independent signals agree** that it is dead.

  Signal 1 (process/log): the last ``Capacity: N/1`` in the server's own log is
    ``0`` -- the server itself reports no session attached.
  Signal 2 (control plane): the last ``cse_`` id the log shows the server
    hosting is ``archived`` in the sessions API.

Either signal alone is unsafe. Signal 1 alone races a server between sessions;
signal 2 alone misreads a *truncated* session list as "no live session" (the
bug fixed alongside this in ``monitor.list_sessions`` -- a single 100-item page
would have marked ~90% of live sessions dead). Requiring agreement means a
disagreement is a KEEP, and every unmapped or unclear case is a KEEP too.

Design note: the classification is a pure function over already-gathered facts
(:func:`classify`), so the kill/keep logic is exhaustively testable without
processes, logs, or network. :func:`sweep` is the only part that touches the
world.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

# ``cse_...`` / ``session_...`` ids as they appear in a server log. The server
# prints the mobile-app URL with a ``session_`` prefix and error lines with a
# ``cse_`` prefix; the suffix is the same id, so we normalise to ``cse_``.
_SESSION_ID_RE = re.compile(r"(?:cse|session)_([A-Za-z0-9]{20,})")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

ARCHIVED = "archived"

#: Reasons a candidate was kept. Logged verbatim so a surprising KEEP is
#: diagnosable from the supervisor log alone.
KEEP_PROTECTED = "protected"
KEEP_BUSY = "capacity!=0"
KEEP_NO_CAPACITY = "no-capacity-line"
KEEP_NO_SESSION_ID = "no-session-id-in-log"
KEEP_SESSION_UNKNOWN = "session-not-in-api"
KEEP_SESSION_LIVE = "session-not-archived"
KEEP_TOO_YOUNG = "younger-than-min-age"
REAP = "reap"


class Candidate(NamedTuple):
    """A running one-shot server plus the facts gathered about it."""
    pid: int
    name: str
    capacity: int            # last "Capacity: N/1" in its log; -1 if none
    session_id: Optional[str]  # last cse_ id the log shows it hosting
    age_secs: float          # how long the process has been up


class Decision(NamedTuple):
    candidate: Candidate
    reap: bool
    reason: str


def parse_session_ids(log_text: str) -> List[str]:
    """Normalised ``cse_`` ids in *log_text*, in first-seen order.

    ANSI escapes are stripped first: the server redraws its status block with
    cursor-movement codes, which can otherwise land mid-token.
    """
    clean = _ANSI_RE.sub("", log_text)
    return ["cse_" + m for m in dict.fromkeys(_SESSION_ID_RE.findall(clean))]


def read_log_session_id(logpath: Path, tail_bytes: int = 200_000) -> Optional[str]:
    """The last session id *logpath* shows the server hosting, or None.

    Only the tail is scanned (a long-lived server's log is large, and the
    current session is always at the end). Returns None when the file is
    missing/unreadable or mentions no id -- both of which classify as KEEP.
    """
    p = Path(logpath)
    if not p.is_file():
        return None
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read()
    except OSError:
        return None
    ids = parse_session_ids(data.decode("utf-8", errors="replace"))
    return ids[-1] if ids else None


def classify(
    cand: Candidate,
    session_status: Optional[str],
    *,
    protected_pids: Iterable[int] = (),
    protected_names: Iterable[str] = (),
    min_age_secs: float = 0.0,
) -> Decision:
    """Pure kill/keep decision for one candidate. Defaults to KEEP.

    *session_status* is the API ``status`` of ``cand.session_id`` (None when the
    id is absent from the session list). Order matters: the protection check
    runs first so a protected pid is never reaped even if every other signal
    says dead.
    """
    def keep(reason: str) -> Decision:
        return Decision(cand, False, reason)

    if cand.pid in set(protected_pids) or cand.name in set(protected_names):
        return keep(KEEP_PROTECTED)
    if cand.age_secs < min_age_secs:
        # A server that just came up may not have logged a Capacity line or
        # attached its session yet; both would read as "dead".
        return keep(KEEP_TOO_YOUNG)
    if cand.capacity < 0:
        return keep(KEEP_NO_CAPACITY)
    if cand.capacity != 0:
        return keep(KEEP_BUSY)                 # signal 1 says a session is attached
    if not cand.session_id:
        return keep(KEEP_NO_SESSION_ID)        # unmappable -> ambiguous -> keep
    if session_status is None:
        return keep(KEEP_SESSION_UNKNOWN)      # absent from API -> keep, never assume
    if session_status != ARCHIVED:
        return keep(KEEP_SESSION_LIVE)         # signals disagree -> keep
    return Decision(cand, True, REAP)          # both signals agree: dead


def plan(
    candidates: Iterable[Candidate],
    session_status_by_id: Dict[str, str],
    *,
    protected_pids: Iterable[int] = (),
    protected_names: Iterable[str] = (),
    min_age_secs: float = 0.0,
) -> List[Decision]:
    """Classify every candidate (pure). Returns decisions in input order."""
    protected_pids = list(protected_pids)
    protected_names = list(protected_names)
    return [
        classify(
            c,
            session_status_by_id.get(c.session_id) if c.session_id else None,
            protected_pids=protected_pids,
            protected_names=protected_names,
            min_age_secs=min_age_secs,
        )
        for c in candidates
    ]


def gather(
    *,
    oneshots: List[Tuple[int, str]],
    logdir: Path,
    read_capacity: Callable[[Path], int],
    process_age: Callable[[int], float],
) -> List[Candidate]:
    """Build candidates from running one-shots + their logs (I/O)."""
    out = []
    for pid, name in oneshots:
        logpath = Path(logdir) / f"{name}.log"
        out.append(Candidate(
            pid=pid,
            name=name,
            capacity=read_capacity(logpath),
            session_id=read_log_session_id(logpath),
            age_secs=process_age(pid),
        ))
    return out


def sweep(
    *,
    oneshots: List[Tuple[int, str]],
    logdir: Path,
    sessions: Optional[List[dict]],
    read_capacity: Callable[[Path], int],
    process_age: Callable[[int], float],
    term: Callable[[int], None],
    log: Callable[[str], None],
    protected_pids: Iterable[int] = (),
    protected_names: Iterable[str] = (),
    min_age_secs: float = 0.0,
    max_per_sweep: Optional[int] = None,
) -> List[Decision]:
    """Gather, classify, and SIGTERM the agreed-dead one-shots.

    *sessions* is the full session list. A None here means the API call failed;
    we abort the whole sweep rather than proceed, because "I couldn't read the
    session list" and "these sessions don't exist" must never collapse into the
    same decision. Returns the decisions that resulted in a TERM.

    Only SIGTERM is ever sent -- no SIGKILL escalation. A one-shot that ignores
    TERM is left alone and reported; killing it hard is a human's call.
    """
    if sessions is None:
        log("reaper: session list unavailable, skipping sweep (fail-safe)")
        return []
    status_by_id = {
        s.get("id"): (s.get("status") or "")
        for s in sessions if isinstance(s, dict) and s.get("id")
    }
    candidates = gather(oneshots=oneshots, logdir=logdir,
                        read_capacity=read_capacity, process_age=process_age)
    decisions = plan(candidates, status_by_id,
                     protected_pids=protected_pids,
                     protected_names=protected_names,
                     min_age_secs=min_age_secs)
    doomed = [d for d in decisions if d.reap]
    if max_per_sweep is not None and len(doomed) > max_per_sweep:
        log(f"reaper: {len(doomed)} dead, capping this sweep at {max_per_sweep}; "
            "the rest go next tick")
        doomed = doomed[:max_per_sweep]
    for d in doomed:
        log(f"reaper: TERM {d.candidate.name} (pid {d.candidate.pid}) "
            f"capacity=0 session={d.candidate.session_id} archived")
        try:
            term(d.candidate.pid)
        except OSError as e:
            log(f"reaper: TERM {d.candidate.name} failed: {e}")
    if doomed:
        log(f"reaper: reaped {len(doomed)} leaked one-shot server(s); "
            f"{len(decisions) - len(doomed)} kept")
    return doomed
