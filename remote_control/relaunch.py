"""Relaunch a Claude Code session as a fresh bridge seeded with prior context.

This is the stand-alone CLI for the same flow the supervisor runs on restart
(:func:`handoff.run_handoff_dispatch`): a stopped or unresumable ``cse_*``
thread cannot accept ``/events`` -- the events API errors -- and a desktop
``/resume`` of its sibling transcript is friction. Instead we **spawn a brand-
new bridge** in the prior session's cwd and submit a derived brief (cwd +
branch + last N user turns from the source transcript) as its first user
turn. The user sees a "ready to work" picker row rather than a broken one.

The CLI composes three existing primitives:

  * :func:`handoff.derive_brief_from_transcript` -- pure brief builder
  * :func:`new_session.main` -- spawn + wait + submit-first-turn (one shot)
  * :func:`handoff.write_handoff_record` -- per-cse JSON ledger that makes
    re-running on the same source idempotent

Source resolution: given ``--from cse_<tail>``, scan
``~/.claude/projects/*/`` for the bridge-worktree dir whose embedded cse id
matches (via :func:`session_fork.cse_id_from_project_dirname`) and pick the
newest ``.jsonl`` inside. The originating cwd is read from the transcript's
first ``cwd`` record (:func:`session_fork.first_cwd`), not derived from the
dir name -- so an ``--into-main`` relocated fork still resolves correctly.

Pure helpers (source resolution, idempotency lookup) live above ``main`` so
the tests don't shell out or hit the network.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Mapping, NamedTuple, Optional, Tuple

from .handoff import (
    DEFAULT_MAX_BRIEF_BYTES,
    DEFAULT_MAX_USER_TURNS,
    DEFAULT_WAIT_TIMEOUT_SECS,
    derive_brief_from_transcript,
    forked_uuids_already_dispatched,
    handoff_record_path,
    write_handoff_record,
)
from .session_fork import (
    cse_id_from_project_dirname,
    default_projects_root,
    first_cwd,
)


class RelaunchSource(NamedTuple):
    """Resolved (transcript path, originating cwd) for a source ``cse_*``."""
    transcript: Path
    cwd: str


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def find_source_transcript(cse_id: str, projects_root: Path) -> Optional[Path]:
    """Newest ``.jsonl`` under any project dir whose name encodes *cse_id*, or
    None. Robust against multiple-fork-into-main races: the source's bridge
    worktree dir is the authoritative match (its name embeds the original
    ``cse_`` tail via :func:`cse_id_from_project_dirname`), and within it the
    newest jsonl is the live session's own transcript."""
    matches: List[Path] = []
    for proj in Path(projects_root).iterdir() if Path(projects_root).is_dir() else []:
        if not proj.is_dir():
            continue
        if cse_id_from_project_dirname(proj.name) != cse_id:
            continue
        matches.extend(proj.glob("*.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def resolve_source(
    *,
    cse_id: Optional[str],
    transcript_arg: Optional[str],
    cwd_arg: Optional[str],
    projects_root: Path,
) -> RelaunchSource:
    """Turn ``--from`` / ``--from-transcript`` + ``--cwd`` into a concrete
    (transcript, cwd) pair. Raises ``FileNotFoundError`` / ``ValueError``
    with the offending argument named so the CLI surface stays diagnosable."""
    if cse_id and transcript_arg:
        raise ValueError("--from and --from-transcript are mutually exclusive")
    if not cse_id and not transcript_arg:
        raise ValueError("one of --from CSE_ID or --from-transcript PATH is required")
    if transcript_arg:
        t = Path(transcript_arg)
        if not t.is_file():
            raise FileNotFoundError(f"--from-transcript not a file: {t}")
        cwd = cwd_arg or first_cwd(t.read_text(errors="ignore").splitlines())
        if not cwd:
            raise ValueError(
                "--cwd is required when the transcript carries no cwd record")
        return RelaunchSource(transcript=t, cwd=cwd)
    # --from CSE_ID
    t = find_source_transcript(cse_id, projects_root)
    if t is None:
        raise FileNotFoundError(
            f"no transcript on disk for {cse_id!r} under {projects_root}")
    cwd = cwd_arg or first_cwd(t.read_text(errors="ignore").splitlines())
    if not cwd:
        raise ValueError(
            f"transcript for {cse_id} carries no cwd record; pass --cwd")
    return RelaunchSource(transcript=t, cwd=cwd)


def already_dispatched(state_dir: Path, forked_marker: str) -> bool:
    """True if a handoff record under *state_dir* already names *forked_marker*
    as its ``forked_uuid``. Used as the idempotency gate before spawning so a
    re-run on the same source is a no-op rather than a duplicate bridge."""
    return forked_marker in forked_uuids_already_dispatched(state_dir)


def subname_for(source_cse: str) -> str:
    """Picker-row tag for the spawned relaunch session. The source's cse_*
    tail (after the ``cse_`` prefix), capped so the rendered
    ``[NICK.host][relaunch-...] auto-spawned`` stays readable."""
    tail = source_cse.removeprefix("cse_")
    return f"relaunch-{tail[:10]}"


# Match a trailing " vN" tag, possibly followed by whitespace. Anchored at end
# so a body like "Resume v2 prep" (v2 in the middle) isn't treated as versioned.
_VERSION_TAIL_RE = re.compile(r"\s+v(\d+)\s*$")


def bump_version(body: str) -> str:
    """Append/increment a trailing ``vN`` tag on *body*. A body without a
    ``vN`` suffix is treated as implicit v1, so the first relaunch becomes v2;
    chained relaunches monotonically advance the number. Empty body -> ``v2``.

    Used to mark the new session's title so the picker shows ``Resume prep v2``
    next to the original ``Resume prep`` (or ``v3`` if the original was already
    ``v2``)."""
    if not body or not body.strip():
        return "v2"
    m = _VERSION_TAIL_RE.search(body)
    if m:
        n = int(m.group(1)) + 1
        return _VERSION_TAIL_RE.sub(f" v{n}", body)
    return f"{body.rstrip()} v2"


# Capture one or two leading ``[token]`` brackets so the spawned session's
# title (already ``[NICK.host][relaunch-...] auto-spawned``) can keep its
# prefix while we swap the body to the bumped source body.
_TITLE_PREFIX_RE = re.compile(r"^\s*(\[[^\]]+\](?:\[[^\]]+\])?)\s*(.*)$")


def swap_title_body(current_title: str, new_body: str) -> str:
    """Replace the body of ``[A] body`` or ``[A][B] body`` with *new_body*,
    preserving the bracket prefixes. If *current_title* has no bracket prefix,
    returns *new_body* verbatim -- the watcher will add the prefix on its next
    pass."""
    m = _TITLE_PREFIX_RE.match(current_title or "")
    if m and m.group(1):
        return f"{m.group(1)} {new_body}".rstrip()
    return new_body.strip()


def compute_relaunch_title(
    *, source_title: Optional[str], spawned_title: Optional[str],
) -> Optional[str]:
    """Build the inherit+bump title to PUT on the relaunched session.

    Pipeline:
      1. Strip the [NICK.host] / [NICK.host][SUB] prefix from *source_title*
         to recover the user-facing body.
      2. Bump its trailing version tag (none -> ``v2``; ``v2`` -> ``v3``; ...).
      3. Splice the bumped body into the spawned session's existing prefix
         (``[NICK.host][relaunch-XXX]``), so the relaunch tag survives.

    Returns None when there is nothing useful to inherit (no source title or
    nothing left after stripping the prefix); the caller leaves the spawn's
    default ``auto-spawned`` title in place rather than overwriting with
    garbage."""
    from .session_titles import strip_prefix
    body = strip_prefix(source_title or "").strip()
    if not body:
        return None
    bumped = bump_version(body)
    return swap_title_body(spawned_title or "", bumped)


USAGE = (
    "usage: python3 -m remote_control relaunch\n"
    "         (--from CSE_ID | --from-transcript PATH)\n"
    "         [--cwd PATH] [--max-turns N] [--max-bytes N]\n"
    "         [--wait-timeout SECS] [--state-dir PATH]\n"
    "         [--show-brief] [--force] [--dry-run]\n"
    "  --from CSE_ID         source session whose transcript seeds the brief.\n"
    "                        resolved via ~/.claude/projects scan.\n"
    "  --from-transcript P   explicit transcript path (skip the scan).\n"
    "  --cwd PATH            override the cwd to spawn into. default: the\n"
    "                        transcript's own first `cwd` record.\n"
    "  --max-turns N         user turns from the prior session to include\n"
    "                        (default 5; matches supervisor's handoff).\n"
    "  --max-bytes N         hard cap on brief size (default 8192).\n"
    "  --wait-timeout SECS   how long to poll for the new cse_* to register\n"
    "                        (default 60; the new-session 30s default missed\n"
    "                        slow registrations).\n"
    "  --state-dir PATH      where the handoff record is written. default:\n"
    "                        $REMOTE_CONTROL_STATE_DIR or ~/.ai-harness.\n"
    "  --show-brief          print the derived brief to stdout.\n"
    "  --force               skip the idempotency gate (write a second record\n"
    "                        for the same source). default: refuse.\n"
    "  --no-retitle          keep the spawn's default `auto-spawned` title.\n"
    "                        default: inherit the source's title + version-bump\n"
    "                        (`Resume prep` -> `Resume prep v2` -> `... v3`).\n"
    "  --dry-run             print brief + spawn argv; no spawn, no record."
)


def _parse_args(argv: List[str]) -> dict:
    opts = {
        "from_cse": None, "from_transcript": None, "cwd": None,
        "max_turns": DEFAULT_MAX_USER_TURNS,
        "max_bytes": DEFAULT_MAX_BRIEF_BYTES,
        "wait_timeout": DEFAULT_WAIT_TIMEOUT_SECS,
        "state_dir": None,
        "show_brief": False, "force": False, "dry_run": False,
        "no_retitle": False, "help": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--from":
            i += 1; opts["from_cse"] = argv[i]
        elif a.startswith("--from="):
            opts["from_cse"] = a.split("=", 1)[1]
        elif a == "--from-transcript":
            i += 1; opts["from_transcript"] = argv[i]
        elif a.startswith("--from-transcript="):
            opts["from_transcript"] = a.split("=", 1)[1]
        elif a == "--cwd":
            i += 1; opts["cwd"] = argv[i]
        elif a.startswith("--cwd="):
            opts["cwd"] = a.split("=", 1)[1]
        elif a == "--max-turns":
            i += 1; opts["max_turns"] = int(argv[i])
        elif a.startswith("--max-turns="):
            opts["max_turns"] = int(a.split("=", 1)[1])
        elif a == "--max-bytes":
            i += 1; opts["max_bytes"] = int(argv[i])
        elif a.startswith("--max-bytes="):
            opts["max_bytes"] = int(a.split("=", 1)[1])
        elif a == "--wait-timeout":
            i += 1; opts["wait_timeout"] = float(argv[i])
        elif a.startswith("--wait-timeout="):
            opts["wait_timeout"] = float(a.split("=", 1)[1])
        elif a == "--state-dir":
            i += 1; opts["state_dir"] = argv[i]
        elif a.startswith("--state-dir="):
            opts["state_dir"] = a.split("=", 1)[1]
        elif a == "--show-brief":
            opts["show_brief"] = True
        elif a == "--force":
            opts["force"] = True
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a == "--no-retitle":
            opts["no_retitle"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif a.startswith("-"):
            raise ValueError(f"unknown arg: {a}")
        else:
            raise ValueError(f"unexpected positional: {a}")
        i += 1
    return opts


def _default_state_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("REMOTE_CONTROL_STATE_DIR") or
                Path(env.get("HOME", "")) / ".ai-harness")


# --------------------------------------------------------------------------- #
# Spawn -- defaults to new_session.main but injectable for tests
# --------------------------------------------------------------------------- #
def default_spawn(
    *, cwd: str, brief: str, subname: str, wait_timeout: float, log,
) -> Optional[str]:
    """Compose ``new-session --prompt-file --no-reply-to --subname`` as one
    process call -- the same primitive ``spawn_and_seed_handoff`` runs, but
    via the public CLI seam so we don't need a :class:`RemoteControlConfig`.

    The brief goes through a NamedTemporaryFile (cleaned up on exit) instead
    of ``--prompt TEXT`` because a multi-KB brief on argv would crowd ps
    listings and risk shell-quoting trouble for callers who script around it.

    Returns the new ``cse_*`` on success, or None on any failure.
    new_session writes its ``session : cse_...`` line to stdout on success;
    we tee it via a sentinel-grep on the captured stdout so this function
    can return it without a second API call."""
    import io
    from contextlib import redirect_stdout
    from . import new_session

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="relaunch-brief-", delete=False,
    ) as f:
        f.write(brief)
        brief_path = f.name
    try:
        argv = [
            "--dir", cwd,
            "--prompt-file", brief_path,
            "--no-reply-to",
            "--subname", subname,
            "--wait-timeout", str(wait_timeout),
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = new_session.main(argv)
        out = buf.getvalue()
        sys.stdout.write(out)
        sys.stdout.flush()
        if rc != 0:
            log(f"new-session exited rc={rc}; see stderr above")
            return None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("session :"):
                return line.split(":", 1)[1].strip()
        log("new-session reported success but no `session : cse_...` line found")
        return None
    finally:
        try:
            os.unlink(brief_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Retitle (inherit source's body + version bump)
# --------------------------------------------------------------------------- #
def default_retitle(
    *, source_cse: Optional[str], new_cse: str, log,
) -> Tuple[bool, Optional[str]]:
    """Fetch *source_cse*'s and *new_cse*'s current titles, compute the
    inherit+bump title, and PUT it on *new_cse*.

    Returns ``(ok, applied_title)``. Best-effort: any API hiccup logs and
    returns ``(False, None)`` so the relaunch overall still succeeds -- the
    title is a visible nice-to-have, not the artifact the user is here for.

    No ``cse_*`` for the source (``--from-transcript`` flow) -> we have no
    title to inherit; the function returns ``(False, None)`` and the caller
    leaves ``auto-spawned`` in place."""
    from .config import UsageLimitConfig
    from .session_titles import set_title
    from .usage_limit import monitor

    if not source_cse:
        return False, None
    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    if not token:
        log("retitle: keychain OAuth missing; leaving auto-spawned title")
        return False, None
    src_code, src_body = monitor.api_request(
        cfg, "GET", f"/sessions/{source_cse}", token)
    if src_code != 200 or not isinstance(src_body, dict):
        log(f"retitle: GET source {source_cse} http={src_code}; skipping")
        return False, None
    new_code, new_body = monitor.api_request(
        cfg, "GET", f"/sessions/{new_cse}", token)
    if new_code != 200 or not isinstance(new_body, dict):
        log(f"retitle: GET new {new_cse} http={new_code}; skipping")
        return False, None
    # Single-session GETs wrap the record in a ``response_shape`` envelope (the
    # ``/sessions`` list endpoint does not -- it returns flat records under
    # ``data``). Mirror the unwrap monitor.still_limit_paused uses so the
    # title field resolves on both shapes.
    src_record = src_body.get("response_shape", src_body)
    new_record = new_body.get("response_shape", new_body)
    title = compute_relaunch_title(
        source_title=src_record.get("title") if isinstance(src_record, dict) else None,
        spawned_title=new_record.get("title") if isinstance(new_record, dict) else None,
    )
    if not title:
        log("retitle: nothing useful to inherit; leaving auto-spawned title")
        return False, None
    put_code, put_body = set_title(cfg, token, new_cse, title)
    if put_code != 200:
        log(f"retitle: PUT failed http={put_code} body={str(put_body)[:200]}")
        return False, None
    return True, title


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(
    argv: Optional[List[str]] = None,
    *,
    spawn: Optional[Callable[..., Optional[str]]] = None,
    retitle: Optional[Callable[..., Tuple[bool, Optional[str]]]] = None,
    projects_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    now_iso: Optional[str] = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    spawn = default_spawn if spawn is None else spawn
    retitle = default_retitle if retitle is None else retitle
    projects_root = (default_projects_root() if projects_root is None
                     else projects_root)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts["help"]:
        print(USAGE)
        return 0

    try:
        src = resolve_source(
            cse_id=opts["from_cse"],
            transcript_arg=opts["from_transcript"],
            cwd_arg=opts["cwd"],
            projects_root=Path(projects_root),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2

    brief = derive_brief_from_transcript(
        src.transcript, cwd=src.cwd,
        max_turns=opts["max_turns"], max_bytes=opts["max_bytes"],
    )

    state_dir = (Path(opts["state_dir"]) if opts["state_dir"]
                 else _default_state_dir(env))
    # The source cse_* is the natural idempotency key: re-running relaunch
    # against the same source should not spawn a second bridge unless the
    # caller explicitly --force's it. handoff records key on forked_uuid; we
    # reuse the source_cse for that field so the gate is the same lookup.
    source_marker = opts["from_cse"] or f"transcript:{src.transcript.name}"
    if not opts["force"] and not opts["dry_run"]:
        if already_dispatched(state_dir, source_marker):
            print(f"relaunch: skip; handoff record already exists for "
                  f"{source_marker} (--force to override)", file=sys.stderr)
            return 0

    sub = subname_for(opts["from_cse"] or src.transcript.stem[:10])

    if opts["dry_run"]:
        print("relaunch: DRY-RUN (drop --dry-run to spawn)")
        print(f"  source     : {opts['from_cse'] or src.transcript}")
        print(f"  transcript : {src.transcript}")
        print(f"  cwd        : {src.cwd}")
        print(f"  subname    : {sub}")
        print(f"  brief      : {len(brief.encode())} bytes, "
              f"{brief.count(chr(10))} lines")
        print(f"  wait       : up to {opts['wait_timeout']:.0f}s for cse_* "
              "registration")
        print("  spawn argv : python3 -m remote_control new-session "
              f"--dir {src.cwd} --prompt-file <tmp> --no-reply-to "
              f"--subname {sub} --wait-timeout {opts['wait_timeout']:.0f}")
        if opts["show_brief"]:
            print("--- brief ---")
            print(brief)
        return 0

    if opts["show_brief"]:
        print("--- brief ---")
        print(brief)
        print("--- end brief ---")

    new_cse = spawn(
        cwd=src.cwd, brief=brief, subname=sub,
        wait_timeout=opts["wait_timeout"], log=log,
    )
    if not new_cse:
        log("relaunch: spawn failed (no new cse_* returned)")
        return 1

    # Inherit + version-bump the source's title. Best-effort: a failed retitle
    # doesn't fail the relaunch -- the brief was delivered, that's the main
    # thing. ``--from-transcript`` skips retitle (no cse to GET).
    if not opts["no_retitle"]:
        ok, applied = retitle(
            source_cse=opts["from_cse"], new_cse=new_cse, log=log,
        )
        if ok:
            print(f"  title  : {applied!r}")

    # Persist the record AFTER spawn success so a crashed spawn doesn't poison
    # the idempotency gate. now_iso is injectable so tests don't pull in
    # datetime as a hidden dependency.
    if now_iso is None:
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec_path = write_handoff_record(
        state_dir,
        new_cse=new_cse,
        forked_uuid=source_marker,
        source_cse=opts["from_cse"] or "(transcript-only)",
        cwd=src.cwd,
        dispatched_at=now_iso,
    )
    print(f"relaunch: {opts['from_cse'] or src.transcript.name} -> "
          f"{new_cse} (record={rec_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
