"""Batch takeover of stale/disconnected sessions: relaunch real work, archive
dead-ends, rename the survivors -- scripting the manual flow a dispatcher
runs when the picker shows a handful of warning-triangle sessions at once.

Per candidate the manual flow was:

  1. find it (stale-by-time OR disconnected -- independently; a session can
     be disconnected but recently active, which ``sessions --stale
     --disconnected`` misses since that filter requires both)
  2. read its last user turn (via :func:`relaunch.resolve_source` +
     :mod:`handoff`'s turn extractors -- there is no ``messages`` subcommand)
  3. classify: a trivial last turn ("hi", empty, few words) -> archive-only;
     anything else -> relaunch
  4. relaunch (:func:`relaunch.main`) or archive the original
     (:func:`usage_limit.monitor.archive_session`)
  5. for a relaunched session, re-title the spawn with the source's own
     ``[NICK...]`` bracket verbatim (:func:`session_titles.set_title` via
     the ``--nick`` escape hatch) -- the titles watcher can't derive a repo
     for an other-host spawn (no local worktree/transcript/log to key off),
     so left alone the new session's title stays bracket-less.

Reuses :func:`relaunch.resolve_source` (source resolution + events-API
fallback), :mod:`handoff`'s turn extractors, and :mod:`session_titles`'s
title-manipulation helpers rather than reimplementing any of them.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import List, NamedTuple, Optional

from . import relaunch
from .config import DEV, UsageLimitConfig
from .handoff import extract_user_turns, extract_user_turns_from_events
from .session_fork import default_projects_root
from .session_list import DEFAULT_STALE_AGE_SECS, is_stale, parse_duration
from .session_titles import apply_prefix, set_title, strip_prefix
from .usage_limit import monitor

# A last turn at or under this many words is judged trivial ("hi", "here",
# empty, or any other short one-liner that isn't real work in progress).
TRIVIAL_MAX_WORDS = 10

RELAUNCH = "relaunch"
ARCHIVE_ONLY = "archive-only"


class Candidate(NamedTuple):
    id: str
    title: str
    last_event_at: str
    last_turn: str  # "" if unrecoverable
    decision: str  # RELAUNCH | ARCHIVE_ONLY
    reason: str


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def is_trivial_turn(text: str) -> bool:
    """A last user turn counts as "no real work in progress" when it's empty
    or at most :data:`TRIVIAL_MAX_WORDS` words ("hi", "here", "ping", a short
    greeting, ...). Real work -- a multi-sentence brief, a pasted error, a
    ticket number plus context -- reliably runs longer than that."""
    return len(text.split()) <= TRIVIAL_MAX_WORDS


def is_takeover_candidate(session: dict, now: float, older_than_secs: int) -> bool:
    """A session needs takeover attention when it's idle AND either stale-by-
    time OR disconnected -- checked independently, not both-required like
    ``sessions --stale --disconnected``. That combined filter misses a
    session that's disconnected but was active recently (last_event_at inside
    the staleness window) -- exactly the case that showed up in the manual
    run this script replaces."""
    if (session.get("worker_status") or "") != "idle":
        return False
    if (session.get("connection_status") or "") == "disconnected":
        return True
    return is_stale(session, now, older_than_secs)


def classify(last_turn: str) -> tuple:
    """(decision, reason) for a candidate's last recovered user turn."""
    if not last_turn:
        return ARCHIVE_ONLY, "no recoverable user turn"
    if is_trivial_turn(last_turn):
        return ARCHIVE_ONLY, f"trivial last turn ({len(last_turn.split())} word(s))"
    return RELAUNCH, "real work in progress"


def clip(text: str, n: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


_LEADING_BRACKET_RE = re.compile(r"^\[([^\]]{1,64})\]")


def leading_nick(title: str) -> Optional[str]:
    """The verbatim text of a title's first bracket (``[DEV.m5][sub] body``
    -> ``"DEV.m5"``), or None if the title has no leading bracket. Used to
    carry the source session's own nickname claim over to its relaunch
    spawn, since the spawn (an other-host bridge from this dispatcher's
    point of view) has no repo signal of its own to derive one."""
    m = _LEADING_BRACKET_RE.match(title or "")
    return m.group(1) if m else None


_RELAUNCH_RESULT_RE = re.compile(r"^relaunch:\s+\S+\s+->\s+(cse_\S+)")


def _extract_relaunched_cse(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        m = _RELAUNCH_RESULT_RE.match(line)
        if m:
            return m.group(1)
    return None


_ONE_LEADING_BRACKET_RE = re.compile(r"\[([^\]]{1,64})\]\s*")


def leading_brackets(title: str) -> List[str]:
    """Every leading bracketed token, in order (``"[A][B] body"`` ->
    ``["A", "B"]``). Used to preserve a relaunch spawn's own ``[relaunch-XXX]``
    subname tag as a chained sub when splicing the source's nickname in."""
    out: List[str] = []
    pos = 0
    text = title or ""
    while True:
        m = _ONE_LEADING_BRACKET_RE.match(text, pos)
        if not m:
            break
        out.append(m.group(1))
        pos = m.end()
    return out


def retitled_with_nick(current_title: str, nick: str) -> str:
    """Splice *nick* onto a relaunch spawn's title as the outermost bracket,
    preserving any existing bracket (the spawn's own ``[relaunch-XXX]``
    subname tag) as a chained sub: ``"[relaunch-abc] auto-spawned"`` +
    ``"DEV.m5"`` -> ``"[DEV.m5][relaunch-abc] auto-spawned"``."""
    subs = [s for s in leading_brackets(current_title) if s != nick]
    body = strip_prefix(current_title)
    return apply_prefix(body, nick, subs=subs)


# --------------------------------------------------------------------------- #
# Last-turn lookup (relaunch's own source resolution, reused)
# --------------------------------------------------------------------------- #
def last_user_turn(
    cse_id: str,
    *,
    projects_root: Path,
    fetch_events,
    log,
) -> str:
    """The most recent recovered user turn for *cse_id*, or ``""`` if none
    could be resolved (no local transcript and the events-API fallback also
    came up empty). Delegates entirely to :func:`relaunch.resolve_source` so
    takeover and relaunch never disagree about where a session's history
    lives."""
    try:
        src = relaunch.resolve_source(
            cse_id=cse_id, transcript_arg=None, cwd_arg=None,
            projects_root=projects_root, fetch_events=fetch_events,
        )
    except (ValueError, FileNotFoundError) as e:
        log(f"{cse_id}: could not resolve source ({e})")
        return ""
    if src.events is not None:
        turns = extract_user_turns_from_events(src.events)
    else:
        turns = extract_user_turns(
            src.transcript.read_text(errors="ignore").splitlines())
    return turns[-1] if turns else ""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control takeover [--dry-run] "
    "[--older-than DUR] [--dev DIR]\n"
    "  Batch-handles stale/disconnected sessions (the picker's warning-\n"
    "  triangle rows): reads each candidate's last user turn, classifies it\n"
    "  as real work in progress (relaunch) or a dead end (archive-only), and\n"
    "  acts -- relaunching + retitling + archiving the original, or just\n"
    "  archiving. Prints a decision + summary table either way.\n"
    "  --dry-run    : classify and print decisions; no relaunch/archive/rename\n"
    "  --older-than : staleness threshold for the time-based check, e.g. 30m,\n"
    "                 2h, 1d (default 1h). A disconnected session is always a\n"
    "                 candidate regardless of this threshold.\n"
    "  --dev        : dev root for bridge-worktree repo lookup (default ~/dev)"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"dry_run": False, "older_than": DEFAULT_STALE_AGE_SECS, "dev": DEV,
            "help": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            opts["dry_run"] = True
        elif a == "--older-than":
            i += 1; opts["older_than"] = parse_duration(argv[i])
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
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts["help"]:
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

    now = time.time()
    active = [s for s in sessions if (s.get("status") or "") != "archived"]
    candidates_raw = [s for s in active
                      if is_takeover_candidate(s, now, opts["older_than"])]

    projects_root = default_projects_root()
    fetch_events = relaunch._default_fetch_events(log)

    results: List[Candidate] = []
    for s in candidates_raw:
        cse_id = s.get("id") or ""
        turn = last_user_turn(cse_id, projects_root=projects_root,
                              fetch_events=fetch_events, log=log)
        decision, reason = classify(turn)
        results.append(Candidate(
            id=cse_id, title=s.get("title") or "",
            last_event_at=s.get("last_event_at") or "",
            last_turn=turn, decision=decision, reason=reason,
        ))

    print(f"{len(results)} takeover candidate(s) "
          f"(of {len(active)} active session(s))\n")

    relaunched = archived = failed = 0
    for c in results:
        print(c.id)
        print(f"    title : {c.title!r}")
        print(f"    last  : {c.last_event_at}")
        print(f"    turn  : {clip(c.last_turn) or '(none recovered)'}")
        print(f"    -> {c.decision} ({c.reason})")

        if opts["dry_run"]:
            print()
            continue

        if c.decision == ARCHIVE_ONLY:
            code, _ = monitor.archive_session(cfg, token, c.id, log)
            if code == 200:
                print(f"    archived {c.id}")
                archived += 1
            else:
                print(f"    FAILED to archive {c.id} (http={code})")
                failed += 1
            print()
            continue

        # RELAUNCH: spawn the fresh bridge, then fix its title (the spawn is
        # an other-host bridge from this dispatcher's perspective -- no local
        # worktree/transcript for the titles watcher to derive a repo from --
        # so carry the source's own bracket verbatim), then archive the
        # original now that its work has a live successor.
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = relaunch.main(["--from", c.id])
        out = buf.getvalue()
        sys.stdout.write(out)
        new_cse = _extract_relaunched_cse(out)
        if rc != 0 or not new_cse:
            print(f"    FAILED to relaunch {c.id} (rc={rc})")
            failed += 1
            print()
            continue
        relaunched += 1

        nick = leading_nick(c.title)
        if nick:
            new_code, new_body = monitor.api_request(
                cfg, "GET", f"/sessions/{new_cse}", token)
            new_record = (new_body or {}).get("response_shape", new_body) \
                if new_code == 200 and isinstance(new_body, dict) else {}
            current_title = (new_record or {}).get("title") or ""
            title = retitled_with_nick(current_title, nick)
            code, resp = set_title(cfg, token, new_cse, title)
            if code == 200:
                print(f"    retitled {new_cse} -> {title!r}")
            else:
                print(f"    retitle FAILED {new_cse} http={code} "
                      f"body={str(resp)[:160]}")

        arc_code, _ = monitor.archive_session(cfg, token, c.id, log)
        if arc_code == 200:
            print(f"    archived original {c.id}")
        else:
            print(f"    FAILED to archive original {c.id} (http={arc_code})")
        print()

    if opts["dry_run"]:
        print(f"(dry run -- re-run without --dry-run to act on these {len(results)})")
        return 0

    print(f"relaunched: {relaunched}  archived: {archived}  failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
