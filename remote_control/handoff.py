"""Dispatch fresh sessions seeded with prior context (handoff, not resume).

PR #30's :mod:`rehydrate` produces local sibling transcripts for orphaned
``cse_`` sessions, surfaced manually via ``/resume``. This module is the
follow-on layer: instead of waiting for the user to pick a sibling out of
the picker, we **spawn a brand-new bridge server** for each one and
inject a first user-turn that quotes the prior session's last few user
messages. The user gets a "ready to work" picker row rather than a
"ready to pick up" one. Trades transcript continuity for reliability --
broken/unresumable threads no longer need to be resumed at all.

The supervisor calls :func:`dispatch_handoffs` right after
:func:`rehydrate.rehydrate_supervisor_orphans` completes; the dispatch
**consumes** the rehydrate's forked-uuid list and never forks itself.

Two existing primitives compose into the whole flow:

  * :mod:`new_session` -- spawn a fresh ``claude remote-control --capacity 1``
    bridge server in a target cwd; ``cse_*`` arrives on registration.
  * :func:`usage_limit.monitor.submit_user_message` -- POST the brief as the
    new session's first user turn (the same path ``sessions submit`` uses).

Idempotency is tracked in ``~/.ai-harness/handoffs/<new-cse>.json`` -- each
record links the spawned ``new_cse`` to the rehydrate-forked ``uuid`` so a
later supervisor restart can skip orphans already handoff'd.

The pure helpers (brief derivation, env-knob, idempotency check) live above
the I/O layer so they unit-test against plain dicts and a fake state dir.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Set, Tuple

# Per-cse handoff record dir, sibling of `rehydrated/` and `oneoffs/` under
# the supervisor state_dir (~/.ai-harness). One JSON file per spawned new cse
# so concurrent writes don't race on a shared file.
HANDOFFS_DIRNAME = "handoffs"

DEFAULT_MAX_USER_TURNS = 5
DEFAULT_MAX_BRIEF_BYTES = 8 * 1024  # 8 KiB hard cap on total brief size

# Knob: "handoff" (default) enables; "off" disables. Forks from #30 still
# happen either way -- only the handoff dispatch layer is gated.
RESUME_ON_RESTART_ENV = "RESUME_ON_RESTART"
RESUME_ON_RESTART_DEFAULT = "handoff"

# How long to wait for the spawned server's cse_* to register on the cloud
# (the OSC-8 hyperlink in its server log). Same default as new_session's CLI.
DEFAULT_WAIT_TIMEOUT_SECS = 60.0


# --------------------------------------------------------------------------- #
# Pure helpers: brief derivation
# --------------------------------------------------------------------------- #
def extract_user_turns(lines: Iterable[str]) -> List[str]:
    """Pull every user-typed turn from a transcript's JSONL lines, in order.

    Filters out the noise that would make a brief unreadable:
      * non-user records (``type != "user"``).
      * meta wrappers (``isMeta=True``) -- system-injected metadata.
      * command/system wrappers whose text begins with ``"<"`` -- slash
        commands and prompt scaffolding the user didn't actually type.
      * tool-result-only turns (no text blocks).

    Returns each turn as a plain string. Identical to the filtering used by
    :func:`manager.last_messages`, but specialized to user turns only.
    """
    out: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "user":
            continue
        if obj.get("isMeta"):
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            continue
        text = text.strip()
        if not text or text.startswith("<"):
            continue
        out.append(text)
    return out


def first_git_branch(lines: Iterable[str]) -> Optional[str]:
    """First non-empty ``gitBranch`` recorded in the transcript. Lets the
    brief name the branch without shelling out to ``git`` (the prior dir
    may also no longer be a usable worktree by the time we run)."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        branch = obj.get("gitBranch")
        if isinstance(branch, str) and branch:
            return branch
    return None


def _clip_utf8(s: str, max_bytes: int) -> str:
    """Truncate *s* so its UTF-8 encoding is at most *max_bytes*. Drops the
    trailing partial codepoint cleanly (``errors='ignore'``)."""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", "ignore")


def format_brief(
    *,
    cwd: str,
    branch: Optional[str],
    user_turns: List[str],
    transcript_path: str,
    max_bytes: int = DEFAULT_MAX_BRIEF_BYTES,
) -> str:
    """Build the handoff brief. Pure -- no I/O.

    Shape (header + body)::

        fresh session reconstructed from a prior unresumable thread;
        transcript at <path>

        cwd: <cwd>
        branch: <branch>

        Last <N> user turn(s) from the prior session (raw):

        --- turn 1 ---
        <text>

        --- turn 2 ---
        <text>
        ...

    If the assembled body exceeds *max_bytes*, the OLDEST turns are dropped
    one at a time until it fits -- the newest turns are the most actionable.
    The header always survives so the new session at minimum knows where to
    look for the prior transcript.
    """
    header = (
        "fresh session reconstructed from a prior unresumable thread; "
        f"transcript at {transcript_path}\n\n"
        f"cwd: {cwd}\n"
        f"branch: {branch or '<unknown>'}\n\n"
    )
    if not user_turns:
        body = "(no user turns recovered from the prior transcript)\n"
        return _clip_utf8(header + body, max_bytes)

    turns = list(user_turns)
    while turns:
        intro = f"Last {len(turns)} user turn(s) from the prior session (raw):\n\n"
        chunks = [f"--- turn {i} ---\n{t}\n" for i, t in enumerate(turns, 1)]
        body = intro + "\n".join(chunks)
        out = header + body
        if len(out.encode("utf-8")) <= max_bytes:
            return out
        turns.pop(0)
    return _clip_utf8(header, max_bytes)


def derive_brief_from_transcript(
    transcript_path: Path,
    *,
    cwd: str,
    max_turns: int = DEFAULT_MAX_USER_TURNS,
    max_bytes: int = DEFAULT_MAX_BRIEF_BYTES,
) -> str:
    """Read *transcript_path* and assemble the handoff brief. Convenience
    wrapper for the common "I have a path, give me a brief" call. A
    missing/unreadable transcript yields a header-only brief -- the new
    session can still ``Read`` the path if it's recoverable."""
    try:
        text = transcript_path.read_text(errors="ignore")
    except OSError:
        return format_brief(
            cwd=cwd, branch=None, user_turns=[],
            transcript_path=str(transcript_path), max_bytes=max_bytes,
        )
    lines = text.splitlines()
    branch = first_git_branch(lines)
    turns = extract_user_turns(lines)[-max_turns:]
    return format_brief(
        cwd=cwd, branch=branch, user_turns=turns,
        transcript_path=str(transcript_path), max_bytes=max_bytes,
    )


# --------------------------------------------------------------------------- #
# Pure helpers: brief derivation from the cloud events API
# --------------------------------------------------------------------------- #
# Mirrors of the JSONL helpers above, but for events returned by
# ``GET /sessions/{id}/events`` (used when no local transcript exists --
# cloud-agent or cross-host bridge sessions, where event records never make
# it into ``~/.claude/projects/``). The events array is newest-first
# (descending ``sequence_num``); the helpers preserve that ordering, then
# the final ``format_brief`` call reverses to the oldest-of-the-last-N
# layout the JSONL path uses.
def _event_user_text(event: dict) -> Optional[str]:
    """Extract user-typed text from one events-API record, or None.

    Filters the events the way :func:`extract_user_turns` filters JSONL:
      * only ``event_type == 'user'`` records (not assistant/system/control)
      * only ``source == 'client'`` (worker-source user events are tool
        results echoed back into the agent, not human input)
      * skip empty / pure tool-result payloads (no text block, or text
        beginning with ``"<"`` -- slash commands and prompt scaffolding)
    """
    if not isinstance(event, dict):
        return None
    if event.get("event_type") != "user":
        return None
    if event.get("source") != "client":
        return None
    payload = event.get("payload") or {}
    content = (payload.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<"):
        return None
    return text


def extract_user_turns_from_events(events: Iterable[dict]) -> List[str]:
    """Pull every user-typed turn out of an events-API page, oldest first.

    Events arrive newest-first from the API (descending ``sequence_num``);
    this returns them oldest-first to match :func:`extract_user_turns`'s
    contract so callers can ``[-max_turns:]`` the result identically."""
    out: List[Tuple[int, str]] = []
    for e in events:
        text = _event_user_text(e)
        if text is None:
            continue
        seq = e.get("sequence_num")
        out.append((seq if isinstance(seq, int) else 0, text))
    out.sort(key=lambda r: r[0])
    return [t for _, t in out]


def first_cwd_from_events(events: Iterable[dict]) -> Optional[str]:
    """Pull ``cwd`` from the first ``system`` event's payload (the
    session-init record Claude Code emits on startup). Mirrors
    :func:`session_fork.first_cwd` for the events-API code path. Returns
    None if the system event is past the fetched window (the caller then
    falls back to ``--cwd`` or session metadata)."""
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("event_type") != "system":
            continue
        payload = e.get("payload") or {}
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def first_git_branch_from_events(events: Iterable[dict]) -> Optional[str]:
    """Pull ``gitBranch`` from the first system event that has one. Cloud
    sessions usually leave this null; we still expose it for symmetry with
    :func:`first_git_branch` so the brief shows the same fields."""
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("event_type") != "system":
            continue
        branch = (e.get("payload") or {}).get("gitBranch")
        if isinstance(branch, str) and branch:
            return branch
    return None


def derive_brief_from_session_events(
    events: List[dict],
    *,
    cwd: str,
    source_label: str,
    max_turns: int = DEFAULT_MAX_USER_TURNS,
    max_bytes: int = DEFAULT_MAX_BRIEF_BYTES,
) -> str:
    """Build the handoff brief from a cloud-events-API page.

    *source_label* substitutes for the ``transcript at <path>`` line --
    typically ``"events api: <cse_id>"`` since there is no local file to
    point the recovered session at."""
    branch = first_git_branch_from_events(events)
    turns = extract_user_turns_from_events(events)[-max_turns:]
    return format_brief(
        cwd=cwd, branch=branch, user_turns=turns,
        transcript_path=source_label, max_bytes=max_bytes,
    )


# --------------------------------------------------------------------------- #
# Idempotency: handoff records
# --------------------------------------------------------------------------- #
def handoff_record_path(state_dir: Path, new_cse: str) -> Path:
    """Per-cse record file. The cse ids are URL-safe alphanumerics so they
    safely double as filenames."""
    return state_dir / HANDOFFS_DIRNAME / f"{new_cse}.json"


def write_handoff_record(
    state_dir: Path,
    *,
    new_cse: str,
    forked_uuid: str,
    source_cse: str,
    cwd: str,
    dispatched_at: str,
) -> Path:
    p = handoff_record_path(state_dir, new_cse)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "new_cse": new_cse,
        "forked_uuid": forked_uuid,
        "source_cse": source_cse,
        "cwd": cwd,
        "dispatched_at": dispatched_at,
    }, indent=2))
    return p


def load_handoff_records(state_dir: Path) -> List[dict]:
    """Read every record under ``<state_dir>/handoffs/*.json``. Corrupt files
    are skipped (not fatal): one bad record shouldn't stop the dispatch from
    reading the rest of the ledger."""
    out: List[dict] = []
    d = state_dir / HANDOFFS_DIRNAME
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            obj = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def forked_uuids_already_dispatched(state_dir: Path) -> Set[str]:
    """Set of forked-uuid values appearing in any handoff record. The
    idempotency gate: a candidate whose forked uuid is in this set has
    already been handoff'd in a prior supervisor incarnation -- skip it."""
    return {
        r["forked_uuid"] for r in load_handoff_records(state_dir)
        if isinstance(r.get("forked_uuid"), str)
    }


# --------------------------------------------------------------------------- #
# Env knob
# --------------------------------------------------------------------------- #
def handoff_enabled(env: Mapping[str, str]) -> bool:
    """``RESUME_ON_RESTART`` knob.

    Two recognized values:
      * ``handoff`` (default) -- run the dispatch.
      * ``off`` -- skip the dispatch entirely. Forks from #30 still land on
        disk, so the manual ``/resume`` flow remains available.

    Anything else (typo, unset) falls through to the default, on the
    principle that the safer-but-noisier path is the better failure mode.
    """
    val = (env.get(RESUME_ON_RESTART_ENV) or RESUME_ON_RESTART_DEFAULT).strip().lower()
    return val != "off"


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
class HandoffCandidate(NamedTuple):
    """A rehydrate-forked orphan ready for handoff dispatch.

    Built from a :func:`rehydrate.read_rehydrated_marker` record -- the
    original cse_, its newly-forked sibling uuid, the cwd to spawn into,
    and the prior transcript path the brief is derived from.
    """
    source_cse: str       # original cse_* (from RehydrateResult.forked)
    forked_uuid: str      # the local sibling uuid (marker["new_sid"])
    run_dir: str          # cwd to spawn the new server in (marker["run_dir"])
    source_path: Path     # the prior transcript (marker["source_path"])


@dataclass
class HandoffResult:
    """What the dispatch pass actually did. Returned for log + assert."""
    dispatched: List[Dict[str, str]] = None
    """Per-success ``{source_cse, new_cse, forked_uuid}`` entries."""
    skipped_already_dispatched: List[str] = None
    """Forked uuids skipped because a prior handoff record covered them."""
    errors: List[Tuple[str, str]] = None
    """``(source_cse, message)`` for spawns that failed."""
    disabled: bool = False
    """True iff ``RESUME_ON_RESTART=off`` short-circuited the whole pass."""

    def __post_init__(self) -> None:
        if self.dispatched is None:
            self.dispatched = []
        if self.skipped_already_dispatched is None:
            self.skipped_already_dispatched = []
        if self.errors is None:
            self.errors = []


def candidates_from_rehydrate_markers(
    state_dir: Path,
    forked_cse_ids: Iterable[str],
) -> List[HandoffCandidate]:
    """Read the per-cse rehydrate markers for each id in *forked_cse_ids*
    and assemble dispatch candidates. Markers missing required fields
    (e.g. a malformed entry from an older format) are silently dropped --
    we can't dispatch without the run_dir + new_sid + source_path."""
    from .rehydrate import read_rehydrated_marker
    out: List[HandoffCandidate] = []
    for cse_id in forked_cse_ids:
        marker = read_rehydrated_marker(state_dir, cse_id)
        if not marker:
            continue
        new_sid = marker.get("new_sid")
        run_dir = marker.get("run_dir")
        source_path = marker.get("source_path")
        if not (isinstance(new_sid, str) and isinstance(run_dir, str)
                and isinstance(source_path, str)):
            continue
        out.append(HandoffCandidate(
            source_cse=cse_id,
            forked_uuid=new_sid,
            run_dir=run_dir,
            source_path=Path(source_path),
        ))
    return out


def dispatch_handoffs(
    *,
    candidates: Iterable[HandoffCandidate],
    state_dir: Path,
    spawn: Callable[["HandoffCandidate", str], Optional[str]],
    now_iso: str,
    log: Callable[[str], None] = lambda _m: None,
    max_turns: int = DEFAULT_MAX_USER_TURNS,
    max_bytes: int = DEFAULT_MAX_BRIEF_BYTES,
) -> HandoffResult:
    """Compose brief + spawn + record-write for each candidate.

    *spawn* is the injected seam: ``spawn(candidate, brief) -> new_cse | None``.
    It encapsulates the side-effectful pieces (spawn a fresh bridge server,
    wait for its cse_*, POST the brief as the first user turn). Returning
    None means dispatch failed for that candidate -- logged, but doesn't
    abort the rest. Exceptions are caught for the same reason.

    Idempotency: candidates whose ``forked_uuid`` appears in any existing
    handoff record are skipped before the brief is even derived.
    """
    res = HandoffResult()
    already = forked_uuids_already_dispatched(state_dir)
    for c in candidates:
        if c.forked_uuid in already:
            res.skipped_already_dispatched.append(c.forked_uuid)
            log(f"handoff: skip {c.source_cse} "
                f"(forked uuid {c.forked_uuid[:8]} already has handoff record)")
            continue
        brief = derive_brief_from_transcript(
            c.source_path, cwd=c.run_dir,
            max_turns=max_turns, max_bytes=max_bytes,
        )
        try:
            new_cse = spawn(c, brief)
        except Exception as e:  # noqa: BLE001 (don't let one bad spawn kill the pass)
            res.errors.append((c.source_cse, f"{type(e).__name__}: {e}"))
            log(f"handoff: FAILED {c.source_cse}: {type(e).__name__}: {e}")
            continue
        if not new_cse:
            res.errors.append((c.source_cse, "spawn returned no cse"))
            log(f"handoff: FAILED {c.source_cse}: spawn returned no cse")
            continue
        write_handoff_record(
            state_dir,
            new_cse=new_cse, forked_uuid=c.forked_uuid,
            source_cse=c.source_cse, cwd=c.run_dir, dispatched_at=now_iso,
        )
        res.dispatched.append({
            "source_cse": c.source_cse,
            "new_cse": new_cse,
            "forked_uuid": c.forked_uuid,
        })
        log(f"handoff: dispatched {c.source_cse} -> {new_cse} (cwd={c.run_dir})")
    return res


# --------------------------------------------------------------------------- #
# Default spawn implementation (composes new_session helpers + monitor)
# --------------------------------------------------------------------------- #
def spawn_and_seed_handoff(
    candidate: HandoffCandidate,
    brief: str,
    *,
    cfg,
    state_dir: Path,
    log: Callable[[str], None],
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECS,
) -> Optional[str]:
    """The real spawn function: launch a fresh ``local-<host>-<8hex>``
    server in ``candidate.run_dir``, wait for its cse_* to register, and
    submit *brief* as the first user turn.

    Composes :mod:`new_session`'s pure helpers (``autogen_name``,
    ``build_argv``, ``wait_for_session_id``) and the same code path
    ``sessions submit`` uses (:func:`monitor.submit_user_message`). Drops a
    oneoff checkpoint so the rehydrate sweep can recover this handoff's
    own transcript if its process dies before clean exit.

    Returns the new cse_* on full success (spawn + register + submit), or
    None on any failure along the way -- the dispatcher logs and continues."""
    from . import new_session
    from .config import UsageLimitConfig
    from .procutil import spawn_env
    from .rehydrate import write_oneoff_checkpoint
    from .usage_limit import monitor

    cwd = Path(candidate.run_dir)
    if not cwd.is_dir():
        log(f"handoff-spawn: run_dir gone: {cwd}")
        return None

    name = new_session.autogen_name(cfg.host)
    err = new_session.name_is_safe(name, cfg.host)
    if err:
        log(f"handoff-spawn: {err}")
        return None

    spawn_mode = new_session.pick_spawn_mode(cwd, lambda p: cwd.joinpath(".git").exists())
    cmd = new_session.build_argv(cfg.claude_bin, name, spawn_mode, cfg.handoff_perm_mode)
    logpath = Path(cfg.logdir) / f"{name}.log"

    if not Path(cmd[0]).exists():
        log(f"handoff-spawn: claude binary not found: {cmd[0]}")
        return None
    logpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = open(logpath, "ab")
    except OSError:
        out = subprocess.DEVNULL

    # Inherit os.environ (a literal None here would crash spawn_env via
    # dict(None)), then strip REMOTE_CONTROL_REPLY_TO -- handoffs are
    # automated, so the spawned worker has no manager to reply to and
    # mustn't inherit a stale sender id from the supervisor's parent.
    # Same class of leak #38 fixes in new_session.
    import os as _os
    child_env = spawn_env(cfg, _os.environ)
    child_env.pop("REMOTE_CONTROL_REPLY_TO", None)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=out, stderr=out,
            start_new_session=True,  # detach: outlive the supervisor restart
        )
    except (OSError, ValueError) as e:
        log(f"handoff-spawn: spawn failed: {e}")
        return None
    finally:
        if hasattr(out, "close"):
            out.close()

    # Same post-crash recovery hook ``new_session`` writes -- the rehydrate
    # sweep can fork this handoff's own transcript if it later dies.
    try:
        from datetime import datetime, timezone
        write_oneoff_checkpoint(
            cfg.state_dir,
            name=name, directory=str(cwd), pid=proc.pid,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except OSError as e:
        log(f"handoff-spawn: checkpoint write failed ({e}); "
            f"this handoff won't be auto-rehydrated on reboot")

    sid = new_session.wait_for_session_id(logpath, wait_timeout)
    if not sid:
        log(f"handoff-spawn: TIMEOUT waiting {wait_timeout}s for cse_* "
            f"registration on {name}; see {logpath}")
        return None

    ulim = UsageLimitConfig.from_env()
    token = monitor.get_token(ulim, log)
    if not token:
        log("handoff-spawn: could not read OAuth token from keychain")
        return None
    code, body = monitor.submit_user_message(ulim, token, sid, brief, log)
    if code != 200:
        log(f"handoff-spawn: submit FAILED {sid} (http={code}) body={str(body)[:200]}")
        return None
    log(f"handoff-spawn: {name} -> {sid} ({len(brief)} chars brief delivered)")
    return sid


# --------------------------------------------------------------------------- #
# Top-level entrypoint (wired into Supervisor._rehydrate_on_startup)
# --------------------------------------------------------------------------- #
def run_handoff_dispatch(
    *,
    cfg,
    forked_cse_ids: Iterable[str],
    env: Mapping[str, str],
    now_iso: str,
    log: Callable[[str], None] = lambda _m: None,
    spawn: Optional[Callable[["HandoffCandidate", str], Optional[str]]] = None,
) -> HandoffResult:
    """Supervisor-facing entrypoint. Honors the ``RESUME_ON_RESTART`` knob,
    builds candidates from rehydrate markers, then runs :func:`dispatch_handoffs`.

    *spawn* is injectable for tests; the production default composes
    :func:`spawn_and_seed_handoff` against *cfg*.
    """
    if not handoff_enabled(env):
        log("handoff: skipped (RESUME_ON_RESTART=off)")
        return HandoffResult(disabled=True)
    candidates = candidates_from_rehydrate_markers(cfg.state_dir, forked_cse_ids)
    if not candidates:
        log("handoff: no candidates from rehydrate")
        return HandoffResult()
    if spawn is None:
        def spawn(c: HandoffCandidate, brief: str) -> Optional[str]:
            return spawn_and_seed_handoff(
                c, brief, cfg=cfg, state_dir=cfg.state_dir, log=log,
            )
    return dispatch_handoffs(
        candidates=candidates,
        state_dir=cfg.state_dir,
        spawn=spawn,
        now_iso=now_iso,
        log=log,
    )
