"""``python3 -m remote_control new-session`` -- spawn a one-shot
``claude remote-control`` server, picker-visible and supervisor-invisible.

Same shape as the launchd supervisor's per-dir servers, but:

  * ``--capacity 1`` so the server hosts at most one session at a time.

    KNOWN CLI GAP -- this does NOT make the server exit when that session
    ends. ``--capacity`` caps *concurrency*, not process lifetime: on session
    end (or crash) the server prints ``Ready · Capacity: 0/1`` and idles
    forever waiting for a session that never comes. This docstring used to
    claim it self-exits; it never did, and every one-shot ever spawned leaked
    a process (168 had piled up on one host by 2026-07-21, burying the real
    dev server in the app's picker). Until a real ``--exit-after-session``
    lands upstream, the supervisor's :mod:`remote_control.oneshot_reaper`
    sweep is what cleans these up -- it TERMs a one-shot only once its log
    reports ``Capacity: 0`` AND its inner ``cse_`` is archived.
  * Name prefix ``local-`` (not ``mm-`` and not ``<host>-``) so neither
    ``procutil._RUNNING_RE`` nor the supervisor's adopt logic can ever pick
    this server up -- the supervisor's regex scans ``<host>-<basename>`` only.
    The historical prefix was ``oneoff-``; the rehydrate sweep + default
    subname strip still recognize ``oneoff-`` so in-flight legacy servers and
    on-disk checkpoints continue to work during the transition.
  * ``--create-session-in-dir`` (the CLI default) is left on, so a session
    row appears in the Claude app picker immediately.

The autogen name is ``local-<nick>-<8hex>``, where ``<nick>`` is this
machine's short host-nick (the same value the titles watcher uses to render
the ``[NICK.host]`` title prefix; see ``config.host_nickname``). The nick
segment keeps picker rows + log filenames disambiguated across parallel
hosts without re-embedding the full hostname.

Use when an agent wants a fresh picker-visible session for itself or another
agent from inside the current session -- e.g. ``start-work`` after claiming
a ticket, or a manager-pattern session dispatching a one-off worker. The
``--wait`` and ``--prompt`` flags collapse the spawn -> register -> first-turn
dance into a single CLI call.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple

from .config import SupervisorConfig, UsageLimitConfig
from .procutil import git_usable_worktree, spawn_env


USAGE = (
    "usage: python3 -m remote_control new-session [--dir PATH] [--name SLUG]\n"
    "                                      [--spawn worktree|same-dir]\n"
    "                                      [--permission-mode MODE]\n"
    "                                      [--inject-into SERVER]\n"
    "                                      [--wait [--wait-timeout SECS]]\n"
    "                                      [--prompt TEXT | --prompt-file PATH]\n"
    "                                      [--reply-to CSE_ID | --no-reply-to]\n"
    "                                      [--subname SLUG | --no-subname]\n"
    "                                      [--task TEXT]\n"
    "                                      [--dry-run]\n"
    "  Spawn a single-session `claude remote-control` server (capacity 1),\n"
    "  picker-visible, supervisor-invisible. NOTE: the server does NOT exit\n"
    "  when its session ends (capacity caps concurrency, not lifetime); the\n"
    "  supervisor's one-shot reaper sweeps the leftovers.\n"
    "    --dir              run in this dir (default: cwd)\n"
    "    --inject-into SERVER  do NOT spawn a fresh server. Instead, attach to\n"
    "                       an already-running named server (e.g. `<host>-dev`)\n"
    "                       by harvesting the cse_* of the session it already\n"
    "                       pre-created (read from `<SERVER>.log` in the\n"
    "                       supervisor's logdir). The pre-created session is not\n"
    "                       immediately submittable, so we WAIT for it to report\n"
    "                       active+connected (re-harvesting the latest log id in\n"
    "                       case it was superseded) and RETRY the submit on HTTP\n"
    "                       409 'not active', then set its [SUB] title. Raises\n"
    "                       that server's Capacity 0->1 rather than starting an\n"
    "                       `local-*` server. Requires --prompt/--prompt-file;\n"
    "                       --wait-timeout bounds the activation wait. Used by the\n"
    "                       supervisor's dispatcher autospawn so the dispatcher\n"
    "                       cse_ lands on the supervisor-owned <host>-dev server\n"
    "                       (the only allowlisted dev server) instead of a fresh\n"
    "                       one-shot. --name/--spawn/--reply-to are ignored.\n"
    "    --name             server name (default: local-<nick>-<8hex>, where\n"
    "                       <nick> is this machine's host-nick); refuses any\n"
    "                       name starting with 'mm-' or '<host>-' (supervisor\n"
    "                       would otherwise reap or adopt it)\n"
    "    --spawn            worktree|same-dir; default auto from git probe\n"
    "    --permission-mode  passed through (default: bypassPermissions, matches supervisor)\n"
    "    --wait             after launch, poll the server's log for its inner\n"
    "                       cse_* session id (printed on cloud registration);\n"
    "                       on success, prints `session : cse_...` to stdout.\n"
    "                       Implied by --prompt and --subname. Default off.\n"
    "    --wait-timeout SECS  how long to poll for registration (default: 30)\n"
    "    --prompt TEXT      submit TEXT as the first user turn after the inner\n"
    "                       session registers (implies --wait)\n"
    "    --prompt-file PATH  same, but read the prompt body from PATH\n"
    "    --reply-to CSE_ID  identify the sender for the spawned worker. Two\n"
    "                       effects: (1) set REMOTE_CONTROL_REPLY_TO=CSE_ID in\n"
    "                       the spawned server's env (survives prompt edits);\n"
    "                       (2) if --prompt is given, prefix the prompt with a\n"
    "                       `[from CSE_ID — reply via send-to-session]` header.\n"
    "                       Default: auto-detect from CLAUDE_CODE_SESSION_ACCESS_TOKEN.\n"
    "    --no-reply-to      skip both env propagation and the prompt header\n"
    "    --task TEXT        description body for the inner session's title, in\n"
    "                       place of the `auto-spawned` placeholder ->\n"
    "                       `[NICK.host][SLUG] TEXT`. Pass this whenever you know\n"
    "                       what you are dispatching: the placeholder says nothing\n"
    "                       about the work and leaves the row reading as an orphan\n"
    "                       in the roster. Implies --wait (needs the cse_id).\n"
    "    --subname SLUG     after registration, set the inner session's title\n"
    "                       to `[NICK.host][SLUG] <task>` so the spawned\n"
    "                       subsession is distinguishable from human-driven\n"
    "                       sessions in the picker / session list. The titles\n"
    "                       watcher preserves the [SLUG] tag across re-renders\n"
    "                       (see session_titles.extract_sub_token). Implies\n"
    "                       --wait. Default: auto-derive from the server name\n"
    "                       (strip `local-`/`oneoff-` prefix); pass --no-subname to skip.\n"
    "    --no-subname       skip setting the [SUB] tag on the inner session\n"
    "    --dry-run          print the command + cwd + log path; don't spawn"
)


# The OSC-8 hyperlink the ``claude`` TUI prints to its stdout (captured into
# the server's log file by our Popen) when its first cloud session connects.
# Same pattern session_titles.build_mm_log_index uses to harvest cse-ids from
# the supervisor's per-server logs; duplicated here so new_session has no
# dependency on session_titles.
# The `?from=cli` suffix used to be appended by the TUI's OSC-8 hyperlink but
# more recent versions emit `https://claude.ai/code/session_<id>` bare. Match
# either: the suffix is optional so both formats are recognized. `[A-Za-z0-9]+`
# stops at the URL's terminator (newline, `?`, or any non-alnum) so the
# capture is exactly the session id.
_SESSION_LINK_RE = re.compile(rb"session_([A-Za-z0-9]+)(?:\?from=cli)?")
_LOG_TAIL_BYTES = 64_000

# Run-anchor marker. The supervisor's ``<host>-dev.log`` is opened in APPEND
# mode (procutil.spawn), so it accumulates ``session_<id>`` links across every
# server (re)start -- including STALE, now-ARCHIVED sessions from prior runs.
# After a supervisor restart the app prints "Environment preserved. Restart
# claude remote-control to reconnect existing sessions." and RECONNECTS a
# preserved (possibly archived) session instead of creating a fresh one, so the
# only ``session_<id>`` in the persistent tail can be a dead id. Harvesting that
# id poisons the dispatcher inject (it can never go active -> a slow re-dispatch
# loop). To anchor harvesting to the CURRENT server run, procutil writes a
# unique marker line to the log immediately before exec; ``extract_session_id``
# only considers ids AT OR AFTER the last marker, so pre-run (stale) ids are
# ignored. The literal prefix is matched; the run token after it is opaque.
_RUN_MARKER_PREFIX = b"### ai-harness run-start "
_RUN_MARKER_RE = re.compile(re.escape(_RUN_MARKER_PREFIX) + rb"[0-9A-Za-z._-]+")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def autogen_name(host: str, rng: Optional[Callable[[int], str]] = None) -> str:
    """``local-<host>-<8hex>``. *host* is the short host-nick from
    ``config.host_nickname`` (the same value the titles watcher uses to render
    the ``[NICK.host]`` title prefix), so picker rows + log filenames stay
    disambiguated across parallel hosts without re-embedding the full
    hostname.

    Historical: the prefix was ``oneoff-`` before #92. Existing in-flight
    ``oneoff-*`` servers + on-disk checkpoints are still recognized by the
    rehydrate sweep and ``default_subname`` so the transition is graceful."""
    gen = rng or (lambda n: secrets.token_hex(n // 2))
    return f"local-{host}-{gen(8)}"


def pick_spawn_mode(directory: Path, git_probe: Callable[[Path], bool]) -> str:
    """``worktree`` if *directory* is a usable git work tree, else ``same-dir``.
    Mirrors discovery.discover's per-dir rule."""
    return "worktree" if git_probe(directory) else "same-dir"


def build_argv(claude_bin: Path, name: str, spawn_mode: str,
               permission_mode: str) -> List[str]:
    """The ``claude remote-control`` command line (pure).

    NOTE: no ``--no-create-session-in-dir`` -- we *want* the session row to
    appear immediately. Capacity is 1, so the server exits after that one
    session ends.
    """
    return [
        str(claude_bin), "remote-control",
        "--name", name,
        "--spawn", spawn_mode,
        "--capacity", "1",
        "--permission-mode", permission_mode,
    ]


def name_is_safe(name: str, host: str) -> Optional[str]:
    """Return None if *name* is safe to use, else an error string.

    Two prefixes are unsafe: ``mm-`` (the conventional supervisor prefix) and
    ``<host>-`` (the ACTUAL prefix the supervisor's ``_RUNNING_RE`` regex
    matches). Either would let the supervisor adopt or reap our one-off based
    on active-dirs membership."""
    if name.startswith("mm-"):
        return f"name must not start with 'mm-' (supervisor would adopt it): {name}"
    if name.startswith(f"{host}-"):
        return (f"name must not start with '{host}-' (supervisor's _RUNNING_RE "
                f"would reap it as an orphaned <host>-* server): {name}")
    return None


def read_log_tail(logpath: Path, tail_bytes: int = _LOG_TAIL_BYTES) -> bytes:
    """Return the last *tail_bytes* of *logpath*, or b'' if it doesn't exist
    yet. Pure (an OSError swallow): the polling caller wants 'no data yet' to
    be indistinguishable from 'log file not created yet'."""
    try:
        with logpath.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            return f.read()
    except OSError:
        return b""


def extract_session_id(tail: bytes) -> Optional[str]:
    """First ``session_<id>?from=cli`` hyperlink in *tail*, returned with the
    ``cse_`` prefix that the API exposes. None if absent.

    Run-anchoring: if *tail* contains one or more run-start markers (see
    ``_RUN_MARKER_PREFIX``), only the region AFTER the LAST marker is searched,
    so ids written by an earlier server run (stale/archived, accumulated in the
    persistent append-mode log) are ignored in favour of the current run's id.
    When no marker is present (older logs, or callers that don't write one) the
    whole tail is searched -- backward-compatible with the pre-anchor behaviour.
    """
    markers = list(_RUN_MARKER_RE.finditer(tail))
    region = tail[markers[-1].end():] if markers else tail
    m = _SESSION_LINK_RE.search(region)
    return f"cse_{m.group(1).decode('ascii')}" if m else None


def wait_for_session_id(
    logpath: Path,
    timeout_secs: float,
    poll_secs: float = 0.5,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    read_tail: Callable[[Path], bytes] = read_log_tail,
) -> Optional[str]:
    """Poll the spawned server's log for the OSC-8 ``session_<id>?from=cli``
    link the ``claude`` TUI prints when its first cloud session connects.
    Returns ``cse_<id>`` on first match, or None if the deadline passes.

    *sleep*, *clock*, *read_tail* are injected so the polling loop unit-tests
    against a controlled timeline without sleeping or hitting the filesystem.
    """
    deadline = clock() + timeout_secs
    while True:
        sid = extract_session_id(read_tail(logpath))
        if sid:
            return sid
        if clock() >= deadline:
            return None
        sleep(poll_secs)


def default_subname(server_name: str) -> Optional[str]:
    """The default subname tag for a spawned subsession: the server name with
    its ``local-`` (or legacy ``oneoff-``) prefix stripped
    (``local-deadbeef`` -> ``deadbeef``; ``oneoff-ff-emails`` -> ``ff-emails``).
    Returns None if stripping leaves nothing usable, in which case the caller
    skips the title-set step."""
    for prefix in ("local-", "oneoff-"):
        if server_name.startswith(prefix):
            tail = server_name[len(prefix):]
            break
    else:
        tail = server_name
    tail = tail.strip()
    return tail or None


def initial_subname_title(
    cwd: Path,
    host: str,
    subname: str,
    dev_root: Path,
    *,
    nicknames_file: Optional[str] = None,
    task: str = "",
) -> Optional[str]:
    """The ``[NICK.host][SUB] <task>`` title to PUT on a freshly-
    registered subsession, or None if the cwd's repo can't be derived (in
    which case the caller skips the title-set; the watcher will still add a
    bare ``[NICK.host]`` once it can, just without the [SUB] tag).

    Dev-ROOT fallback: when *cwd* IS the dev root itself (not any repo under
    it), render a repo-less ``[DEV.<host>]`` prefix instead of returning None.
    The supervisor's dispatcher injects with ``cwd == dev_root`` (e.g.
    ``/Users/me/dev``), which is not a git repo, so the old repo-only logic
    returned None and the ``[dispatcher]`` tag got silently skipped (the live
    ``could not derive repo … skipping [dispatcher] title tag`` log). The
    fallback gives that session a sensible ``[DEV.<host>][dispatcher]`` tag.

    *task* is the description body. It defaults to the literal
    ``auto-spawned`` -- a placeholder that says nothing about the work and, left
    unreplaced, is half of why dispatcher-spawned workers read as orphans in the
    roster (see docs/session-naming-model.md). A spawner that knows what it is
    dispatching should pass ``--task`` so the row is meaningful from the first
    moment, rather than relying on the worker to retitle itself later.

    Lazy-imports session_titles so new_session stays light when the
    subsession-title path isn't exercised (and so a circular import can't
    bite us through cli.py)."""
    from .session_titles import (
        NICKNAMES_FILE, apply_prefix, build_nickname_map, render_prefix,
        repo_from_cwd, repo_from_worktree_path, session_values, title_format,
    )
    # Both paths get resolve()'d so the macOS /private/var symlink doesn't break
    # the relative_to() check inside repo_from_cwd (the supervisor resolves cwd
    # too, so this keeps both sides of the comparison canonical).
    body = (task or "").strip() or "auto-spawned"
    cwd_s = str(Path(cwd).resolve())
    dev_s = str(Path(dev_root).resolve())
    repo = repo_from_worktree_path(cwd_s) or repo_from_cwd(cwd_s, dev_s)
    try:
        file_text = Path(nicknames_file or NICKNAMES_FILE).read_text()
    except OSError:
        file_text = ""
    nmap = build_nickname_map(file_text,
                              os.environ.get("SESSION_TITLE_NICKNAMES", ""))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    if not repo:
        # Only the dev root itself gets the fallback; any other unresolvable
        # cwd still returns None (unchanged behaviour for the spawn path).
        if cwd_s != dev_s:
            return None
        # Fixed ``DEV`` nick (not derived from a repo basename), host_local=True
        # (we ARE running here). repo="dev" only feeds the {repo} token; {nick}
        # is forced to DEV so the prefix reads [DEV.<host>] regardless of map.
        vals = session_values({"id": ""}, "dev", nmap,
                              host=host, host_local=True, branch="")
        vals["nick"] = "DEV"
        token = render_prefix(template, vals)
        return apply_prefix(body, token, sub=subname)
    # No id yet (the title PUT itself doesn't need it; the ``{id}``/``{shortid}``
    # template tokens get an empty value), host_local=True (we ARE running here).
    vals = session_values({"id": ""}, repo, nmap,
                          host=host, host_local=True, branch="")
    token = render_prefix(template, vals)
    return apply_prefix(body, token, sub=subname)


def own_session_id_from_env(env: Mapping[str, str]) -> Optional[str]:
    """The current process's own ``cse_*`` id, decoded from the desktop app's
    ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` JWT (``session_id`` claim). Local
    import to keep this module light when callers don't need the auto-detect
    path (and to avoid importing session_list at module load -- it would pull
    in the API client transitively)."""
    from .session_list import own_session_id_from_env as _own
    return _own(dict(env))


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _parse_args(argv: List[str]) -> dict:
    opts: dict = {
        "dir": None, "name": None, "spawn": None,
        "permission_mode": "bypassPermissions", "dry_run": False, "help": False,
        "wait": False, "wait_timeout": 30.0,
        "prompt": None, "prompt_file": None,
        "reply_to": None, "no_reply_to": False, "task": None,
        "subname": None, "no_subname": False,
        "inject_into": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dir":
            i += 1; opts["dir"] = argv[i]
        elif a == "--name":
            i += 1; opts["name"] = argv[i]
        elif a == "--spawn":
            i += 1; opts["spawn"] = argv[i]
        elif a == "--inject-into":
            i += 1; opts["inject_into"] = argv[i]
        elif a == "--permission-mode":
            i += 1; opts["permission_mode"] = argv[i]
        elif a == "--wait":
            opts["wait"] = True
        elif a == "--wait-timeout":
            i += 1; opts["wait_timeout"] = float(argv[i])
        elif a == "--prompt":
            i += 1; opts["prompt"] = argv[i]
        elif a == "--prompt-file":
            i += 1; opts["prompt_file"] = argv[i]
        elif a == "--reply-to":
            i += 1; opts["reply_to"] = argv[i]
        elif a == "--no-reply-to":
            opts["no_reply_to"] = True
        elif a == "--task":
            i += 1; opts["task"] = argv[i]
        elif a == "--subname":
            i += 1; opts["subname"] = argv[i]
        elif a == "--no-subname":
            opts["no_subname"] = True
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def _resolve_prompt(opts: dict) -> Optional[str]:
    """Return the prompt body from --prompt or --prompt-file, or None. Raises
    ValueError on conflict / unreadable file / empty body."""
    if opts["prompt"] is not None and opts["prompt_file"] is not None:
        raise ValueError("--prompt and --prompt-file are mutually exclusive")
    if opts["prompt"] is not None:
        body = opts["prompt"]
    elif opts["prompt_file"] is not None:
        try:
            body = Path(opts["prompt_file"]).read_text()
        except OSError as e:
            raise ValueError(f"--prompt-file unreadable: {e}") from e
    else:
        return None
    if not body.strip():
        raise ValueError("prompt body is empty")
    return body


def _post_subname_title(
    sid: str,
    cwd: Path,
    host: str,
    subname: str,
    dev_root: Path,
    log,
    *,
    task: str = "",
    set_title: Optional[Callable[..., Any]] = None,
    get_token: Optional[Callable[..., Any]] = None,
) -> None:
    """PUT the freshly-spawned session's title to ``[NICK.host][SUB] auto-spawned``
    so the spawned subsession is distinguishable in the picker. Best-effort:
    failures are logged, not raised -- the spawn + prompt is what matters, the
    subname tag is a nice-to-have.

    The titles watcher's plan_renames re-runs every ~10min and would normally
    strip our [SUB] tag, but it now preserves it across passes via
    session_titles.extract_sub_token, so the tag sticks until the subsession
    ends."""
    title = initial_subname_title(cwd, host, subname, dev_root, task=task)
    if not title:
        log(f"could not derive repo for {cwd}; skipping [{subname}] title tag")
        return
    from .session_titles import set_title as _set_title
    set_title = set_title or _set_title
    get_token = get_token or _get_token_default
    cfg = UsageLimitConfig.from_env()
    token = get_token(cfg, log)
    if not token:
        log(f"could not set [{subname}] title: keychain OAuth missing")
        return
    code, body = set_title(cfg, token, sid, title)
    if code == 200:
        print(f"  title  : {title!r}")
    else:
        log(f"FAILED to set subsession title (http={code}): {str(body)[:200]}")


def _get_token_default(cfg, log):
    """Default keychain-OAuth fetcher; injectable via the ``get_token`` arg of
    the helpers above so tests don't have to mock the monitor module."""
    from .usage_limit import monitor
    return monitor.get_token(cfg, log)


def _submit_prompt(
    sid: str,
    message: str,
    log,
    *,
    submit: Optional[Callable[..., Any]] = None,
    get_token: Optional[Callable[..., Any]] = None,
) -> int:
    """POST the first user turn into *sid*. Hooks into the same code path as
    ``sessions submit`` (same wrapped-event body, same auth) so spawn + first
    turn is genuinely one-shot, not two CLIs glued together. Returns
    exit-status semantics (0 on 200, 1 otherwise)."""
    from .usage_limit import monitor
    submit = submit or monitor.submit_user_message
    get_token = get_token or monitor.get_token
    cfg = UsageLimitConfig.from_env()
    token = get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    code, body = submit(cfg, token, sid, message, log)
    if code == 200:
        print(f"submitted {sid} ({len(message)} chars)")
        return 0
    log(f"FAILED {sid} (http={code}) body={str(body)[:200]}")
    return 1


# A freshly ``--create-session-in-dir`` pre-created session is NOT immediately
# submittable: the API 409s ("Session is not active") until the dev server's
# TUI actually attaches it. And the log's FIRST session_<id> link can be a
# transient pre-create that gets SUPERSEDED by a later, different active session
# (observed live: 015x… at byte 151 -> 01An… at byte 5.5MB, the latter being the
# one that reached status=active/connection=connected). So the inject path must
# (a) re-harvest the latest log id on each pass (catch the supersession), and
# (b) poll session-state until active+connected, retrying the submit on 409.
_ACTIVATION_POLL_SECS = 1.0       # gap between activation/409 retries
_SUBMITTABLE_STATUS = "active"
_SUBMITTABLE_CONN = "connected"

# Terminal session states: a session in one of these will NEVER become
# submittable, so waiting for ``active`` is pointless. Observed live: a restart
# reconnected a preserved, already-ARCHIVED session, whose id was the only one
# in the persistent log; the inject burned the full 45s polling for it to go
# active (it never could) and the supervisor re-dispatched every tick. When the
# harvested id is in a dead state we bail FAST (don't burn the window) and -- if
# no live id supersedes it -- surface a distinct rc so the supervisor can recycle
# the dev server (drop the preserved session) rather than loop. Lower-cased
# compare; the set is generous so unknown dead variants also short-circuit.
_DEAD_STATUSES = frozenset({
    "archived", "deleted", "completed", "failed", "expired",
    "cancelled", "canceled", "terminated", "closed", "ended",
})

# Distinct rc for "the harvested session is dead/terminal" so the supervisor can
# tell a poisoned-log failure (recycle the dev server) from a benign timeout.
RC_DEAD_SESSION = 2


def submit_active_with_retry(
    *,
    initial_sid: str,
    message: str,
    logpath: Path,
    timeout_secs: float,
    log,
    submit: Optional[Callable[..., Any]] = None,
    get_token: Optional[Callable[..., Any]] = None,
    fetch_state: Optional[Callable[..., Any]] = None,
    read_tail: Callable[[Path], bytes] = read_log_tail,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    poll_secs: float = _ACTIVATION_POLL_SECS,
) -> Tuple[int, str]:
    """Submit *message* into the dev server's CURRENT active session, waiting
    for it to become submittable and retrying on HTTP 409.

    Loops until *timeout_secs* elapses:

      1. Re-harvest the latest ``session_<id>`` from *logpath* (the server may
         have superseded the pre-created id with a different active one).
      2. ``GET /sessions/{id}`` -- only POST when ``status == active`` and
         ``connection_status == connected``; otherwise wait and re-poll.
      3. POST the turn. On 200 -> success. On 409 ("not active") -> the session
         raced us / got superseded; wait and re-loop (re-harvesting the id).
         On any other non-200 -> a real error; fail fast (don't burn the window
         retrying an auth/5xx error).

    Returns ``(rc, final_sid)`` where rc is 0 on success, 1 on timeout/failure,
    and ``RC_DEAD_SESSION`` (2) when the only harvestable id is in a terminal
    state (archived/deleted/...). A permanent not-active gives a clean rc=1 at
    the deadline -- never a hang. A dead harvested id returns FAST (no full-window
    poll), so a poisoned log can't cost the whole timeout per tick.
    """
    from .usage_limit import monitor
    submit = submit or monitor.submit_user_message
    get_token = get_token or monitor.get_token
    fetch_state = fetch_state or monitor.fetch_session_state
    # Resolve clock/sleep at call time (not as def-time defaults) so a test can
    # patch ``new_session.time`` and have the change take effect here.
    sleep = sleep if sleep is not None else time.sleep
    clock = clock if clock is not None else time.monotonic
    cfg = UsageLimitConfig.from_env()
    token = get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1, initial_sid

    deadline = clock() + timeout_secs
    sid = initial_sid
    attempts = 0
    while True:
        # (1) Re-harvest: the active session id may differ from the one we first
        # saw (the pre-created link gets superseded). Prefer the latest log id.
        latest = extract_session_id(read_tail(logpath))
        if latest and latest != sid:
            log(f"inject: target session superseded {sid} -> {latest}")
            sid = latest

        # (2) Gate on submittable state. A None code == transport blip: treat as
        # "unknown", fall through to attempting the submit (the POST itself is
        # the authoritative check) rather than spinning silently.
        code, status, conn = fetch_state(cfg, token, sid, log)

        # (2a) FAST dead-id bail. A terminal session (archived/deleted/...) will
        # never go active; polling it to the deadline is the slow-loop bug. If
        # the CURRENT id is dead AND no live id supersedes it on a re-harvest,
        # give up immediately with RC_DEAD_SESSION so the supervisor recycles the
        # dev server instead of re-dispatching into the same poisoned log.
        if code == 200 and status.lower() in _DEAD_STATUSES:
            fresh = extract_session_id(read_tail(logpath))
            if not fresh or fresh == sid:
                log(f"inject: harvested session {sid} is terminal "
                    f"(status={status!r}); no live session in this run -- "
                    f"bailing fast for dev-server recycle")
                return RC_DEAD_SESSION, sid
            # A different, possibly-live id appeared -- adopt it and re-loop.
            log(f"inject: dead session {sid} superseded by {fresh}; retrying")
            sid = fresh
            continue

        submittable = (code != 200) or (
            status == _SUBMITTABLE_STATUS and conn == _SUBMITTABLE_CONN)
        if not submittable:
            if clock() >= deadline:
                log(f"inject: TIMEOUT waiting for {sid} to become active "
                    f"(last status={status!r} conn={conn!r}, {attempts} submit attempts)")
                return 1, sid
            sleep(poll_secs)
            continue

        # (3) Authoritative attempt.
        attempts += 1
        scode, body = submit(cfg, token, sid, message, log)
        if scode == 200:
            print(f"submitted {sid} ({len(message)} chars)")
            return 0, sid
        if scode == 409:
            # Raced the activation, or the session got superseded between the
            # state-check and the POST. Re-loop (re-harvest + re-poll).
            if clock() >= deadline:
                log(f"inject: TIMEOUT -- {sid} still 409 'not active' after "
                    f"{attempts} attempts over {timeout_secs}s")
                return 1, sid
            log(f"inject: {sid} 409 (not active yet); retrying")
            sleep(poll_secs)
            continue
        # Any other non-200 is a real error (auth/5xx/bad request) -- don't burn
        # the whole window retrying it.
        log(f"inject: FAILED {sid} (http={scode}) body={str(body)[:200]}")
        return 1, sid


def inject_into_server(
    *,
    server_name: str,
    cwd: Path,
    cfg: SupervisorConfig,
    prompt_body: str,
    subname: Optional[str],
    wait_timeout: float,
    log,
    task: str = "",
    waiter: Optional[Callable[..., Optional[str]]] = None,
    submit: Optional[Callable[..., Any]] = None,
    get_token: Optional[Callable[..., Any]] = None,
    set_title: Optional[Callable[..., Any]] = None,
    fetch_state: Optional[Callable[..., Any]] = None,
) -> int:
    """Attach to an ALREADY-RUNNING named server instead of spawning a fresh
    ``local-*`` server.

    The supervisor's ``<host>-dev`` server comes up with a *pre-created* session
    (``--create-session-in-dir``), which writes the same ``session_<id>`` OSC-8
    link to ``<server_name>.log`` that a one-off spawn does. We harvest that
    cse_, set its ``[SUB]`` title, and submit the first turn into it -- raising
    the EXISTING ``<host>-dev`` server's Capacity 0->1, with no second server.

    This is the fix for the dispatcher runaway: the old ``new-session`` default
    spawned a brand-new ``local-*`` server (capacity 1) whose cse_ attached to
    THAT server, leaving ``<host>-dev`` at Capacity 0 forever -- so the
    supervisor re-dispatched every tick (a one-shot storm). By injecting into
    the running dev server, the dispatch actually occupies the slot the
    supervisor is gating on, so ``should_dispatch_dispatcher``'s ``cap==0``
    guard then correctly STOPS re-dispatching.

    ACTIVATION (the 409 fix): the harvested log id is often the dev server's
    *pre-created* session, which is NOT yet submittable -- a bare POST gets
    HTTP 409 "Session is not active" -- and that pre-created id can be
    SUPERSEDED by a later, different active session (the same id appears later
    in the log). So instead of submitting the first harvested id once, we hand
    off to ``submit_active_with_retry``: it re-harvests the latest log id, waits
    for ``status=active``/``connection=connected``, and retries the POST on 409
    until the turn is genuinely accepted (bounded by *wait_timeout*). It returns
    success ONLY when the API 200s the submit.

    Returns 0 on success (prompt submitted), non-zero on any failure. Failures
    log and return -- the supervisor's tick loop must keep running.
    """
    waiter = wait_for_session_id if waiter is None else waiter
    logpath = Path(cfg.logdir) / f"{server_name}.log"
    if not logpath.is_file():
        log(f"inject: server log not found: {logpath} "
            f"(is {server_name} running with a pre-created session?)")
        return 1
    sid = waiter(logpath, wait_timeout)
    if not sid:
        log(f"inject: TIMEOUT waiting {wait_timeout}s for {server_name}'s "
            f"pre-created session id in {logpath}")
        return 1
    print(f"inject: target server {server_name} session {sid}")

    # Wait for an active+connected session and submit (retrying on 409). This
    # resolves the FINAL session id (which may differ from the first-harvested
    # one), so the title PUT below targets the same session the turn landed in.
    rc, sid = submit_active_with_retry(
        initial_sid=sid, message=prompt_body, logpath=logpath,
        timeout_secs=wait_timeout, log=log,
        submit=submit, get_token=get_token, fetch_state=fetch_state,
    )
    if rc != 0:
        return rc

    if subname:
        _post_subname_title(sid, cwd, cfg.host, subname, cfg.dev, log,
                            task=task,
                            set_title=set_title, get_token=get_token)

    # Emit the same `session : cse_...` sentinel a one-off spawn prints so
    # callers (relaunch's stdout-grep, the supervisor's log) can tee the id.
    print(f"  session: {sid}")
    return 0


def main(argv: Optional[List[str]] = None, popen=None, git_probe=None,
         rng: Optional[Callable[[int], str]] = None,
         env: Optional[Mapping[str, str]] = None,
         waiter: Optional[Callable[..., Optional[str]]] = None,
         submit: Optional[Callable[..., Any]] = None,
         get_token: Optional[Callable[..., Any]] = None,
         set_title: Optional[Callable[..., Any]] = None,
         fetch_state: Optional[Callable[..., Any]] = None) -> int:
    popen = subprocess.Popen if popen is None else popen
    git_probe = git_usable_worktree if git_probe is None else git_probe
    env = os.environ if env is None else env
    waiter = wait_for_session_id if waiter is None else waiter
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

    try:
        prompt_body = _resolve_prompt(opts)
    except ValueError as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2

    if opts["reply_to"] and opts["no_reply_to"]:
        print(f"--reply-to and --no-reply-to are mutually exclusive\n{USAGE}",
              file=sys.stderr)
        return 2
    if opts["subname"] and opts["no_subname"]:
        print(f"--subname and --no-subname are mutually exclusive\n{USAGE}",
              file=sys.stderr)
        return 2

    # --task, like --subname, is applied to the registered session, so it needs
    # the cse_id and therefore implies --wait too.
    # A prompt requires the cse_id, so it implies --wait. (--subname also
    # needs the cse_id to PUT the title, but it stays opt-in via --wait/--prompt
    # so the default fire-and-forget spawn doesn't grow a 30s polling loop.)
    want_wait = opts["wait"] or prompt_body is not None or bool(opts["task"])

    cfg = SupervisorConfig.from_env(env)
    cwd = Path(opts["dir"] or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"target dir does not exist: {cwd}", file=sys.stderr)
        return 2

    # --inject-into: attach to an already-running named server (the supervisor's
    # <host>-dev) instead of spawning a fresh local-* server. Short-circuits the
    # whole spawn path -- we never start a `claude remote-control`, we just
    # harvest the dev server's pre-created session id and submit into it.
    if opts["inject_into"]:
        if prompt_body is None:
            print(f"--inject-into requires --prompt or --prompt-file\n{USAGE}",
                  file=sys.stderr)
            return 2
        # Subname defaults like the spawn path, but derived from the TARGET
        # server name (strip the host prefix) rather than an autogen oneoff name.
        inj_subname: Optional[str] = None
        if not opts["no_subname"]:
            inj_subname = opts["subname"] or default_subname(opts["inject_into"])
        if opts["dry_run"]:
            # Harvest WHAT we WOULD inject into without doing it: same log path
            # and waiter the live path uses, but no submit / no title PUT. The
            # harvested cse_ is best-effort -- if the log isn't there yet we
            # still print everything else (server, prompt, intended title) so
            # the operator can see the planned action.
            logpath = Path(cfg.logdir) / f"{opts['inject_into']}.log"
            harvested = waiter(logpath, 0.0) if logpath.is_file() else None
            print("new-session: DRY-RUN inject (drop --dry-run to submit)")
            print(f"  target : {opts['inject_into']}")
            print(f"  log    : {logpath}")
            print(f"  session: {harvested or '(none harvested — server log absent or empty)'}")
            if inj_subname:
                title = initial_subname_title(
                    cwd, cfg.host, inj_subname, cfg.dev) if harvested else None
                print(f"  subname: {inj_subname}")
                if title:
                    print(f"  title  : {title!r}")
            preview = prompt_body.strip().splitlines()[0][:80]
            print(f"  prompt : {len(prompt_body)} chars; first line: {preview!r}")
            return 0
        return inject_into_server(
            server_name=opts["inject_into"],
            cwd=cwd, cfg=cfg, prompt_body=prompt_body, subname=inj_subname,
            wait_timeout=opts["wait_timeout"], log=log, waiter=waiter,
            task=opts.get("task") or "",
            submit=submit, get_token=get_token, set_title=set_title,
            fetch_state=fetch_state,
        )

    name = opts["name"] or autogen_name(cfg.host, rng)
    err = name_is_safe(name, cfg.host)
    if err:
        print(err, file=sys.stderr)
        return 2

    # Subname resolution: explicit > auto-derive-from-name > none. --no-subname
    # short-circuits to None regardless. Only kicks in when we're already
    # waiting (--wait or --prompt) -- the title PUT needs the cse_id, and we
    # don't want a plain `new-session` call to grow a 30s polling loop just
    # for an aesthetic title tag.
    subname: Optional[str] = None
    if want_wait and not opts["no_subname"]:
        subname = opts["subname"] or default_subname(name)
    elif opts["subname"] and not want_wait:
        log("--subname requires --wait or --prompt to set the title; ignoring")

    spawn_mode = opts["spawn"] or pick_spawn_mode(cwd, git_probe)
    if spawn_mode not in ("worktree", "same-dir"):
        print(f"--spawn must be worktree|same-dir, got {spawn_mode!r}",
              file=sys.stderr)
        return 2

    # Reply-to resolution: explicit > auto-detect from this process's JWT >
    # nothing. With --no-reply-to we skip both env propagation and any prompt
    # header. The fall-through (no reply id detected, no --no-reply-to) is a
    # warning, not a failure, so unattended scripted callers still work.
    reply_to: Optional[str] = None
    if not opts["no_reply_to"]:
        reply_to = opts["reply_to"] or own_session_id_from_env(env)
        if not reply_to and not opts["reply_to"]:
            log("no sender cse_id detected (CLAUDE_CODE_SESSION_ACCESS_TOKEN unset); "
                "spawned worker won't know who to reply to — pass --reply-to CSE_ID "
                "or --no-reply-to to silence this")

    cmd = build_argv(cfg.claude_bin, name, spawn_mode, opts["permission_mode"])
    logpath = Path(cfg.logdir) / f"{name}.log"

    if opts["dry_run"]:
        print("new-session: DRY-RUN (drop --dry-run to launch)")
        print(f"  name   : {name}")
        print(f"  cwd    : {cwd}")
        print(f"  spawn  : {spawn_mode}")
        print(f"  log    : {logpath}")
        if reply_to:
            print(f"  reply-to: {reply_to}  (REMOTE_CONTROL_REPLY_TO in child env)")
        if subname:
            print(f"  subname: {subname}  ([NICK.host][{subname}] auto-spawned title)")
        if want_wait:
            print(f"  wait   : up to {opts['wait_timeout']}s for cse_* registration")
        if prompt_body is not None:
            preview = prompt_body.strip().splitlines()[0][:80]
            print(f"  prompt : {len(prompt_body)} chars; first line: {preview!r}")
        print(f"  command: {' '.join(cmd)}")
        return 0

    if not Path(cmd[0]).exists():
        print(f"claude binary not found: {cmd[0]}", file=sys.stderr)
        return 1
    logpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = open(logpath, "ab")
    except OSError:
        out = subprocess.DEVNULL

    child_env = spawn_env(cfg, env)
    if reply_to:
        # Survives any user-side editing of the prompt header on the receiving
        # session: the worker can read REMOTE_CONTROL_REPLY_TO from its env at
        # any time and reply, even if the human strips the `[from ...]` line.
        child_env["REMOTE_CONTROL_REPLY_TO"] = reply_to
    else:
        # The PARENT process may itself be a bridge worker whose env was
        # seeded with REMOTE_CONTROL_REPLY_TO (its own manager's id, per
        # the same `--reply-to` propagation above). Inheriting it via
        # spawn_env would silently retarget the grandchild at our parent's
        # manager -- exactly the leak surfaced in #38. Strip it so the
        # spawned worker has no stale reply target.
        child_env.pop("REMOTE_CONTROL_REPLY_TO", None)

    try:
        proc = popen(
            cmd, cwd=str(cwd),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=out, stderr=out,
            # Detach: outlive the caller (e.g. the agent that triggered this).
            start_new_session=True,
        )
    except (OSError, ValueError) as e:
        print(f"spawn failed: {e}", file=sys.stderr)
        return 1
    finally:
        if hasattr(out, "close"):
            out.close()

    # Drop a checkpoint so the supervisor sweep can fork our transcript for the
    # /resume picker if our process dies before clean exit (reboot, OOM, etc.).
    # The checkpoint lives at ~/.ai-harness/oneoffs/<name>.json -- the sweep
    # reads from the same SupervisorConfig.state_dir. The dirname stays
    # ``oneoffs/`` post-#92 rename for back-compat with legacy checkpoints;
    # only the server NAME prefix changed (``oneoff-`` -> ``local-``).
    try:
        from datetime import datetime, timezone
        from .rehydrate import write_oneoff_checkpoint
        write_oneoff_checkpoint(
            cfg.state_dir,
            name=name, directory=str(cwd), pid=proc.pid,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except OSError as e:
        # A failed checkpoint is non-fatal: the spawn succeeded, we just lose
        # the post-crash recovery for THIS one-off. Surface it so the user
        # knows recovery won't kick in.
        print(f"new-session: WARN -- checkpoint write failed ({e}); "
              f"this oneoff won't be auto-rehydrated on reboot", file=sys.stderr)

    print(f"new-session: launched (pid {proc.pid})")
    print(f"  name   : {name}")
    print(f"  cwd    : {cwd}")
    print(f"  spawn  : {spawn_mode}")
    print(f"  log    : {logpath}")
    if reply_to:
        print(f"  reply-to: {reply_to}")
    print(f"  command: {' '.join(cmd)}")

    if not want_wait:
        return 0

    sid = waiter(logpath, opts["wait_timeout"])
    if not sid:
        print(f"new-session: TIMEOUT waiting {opts['wait_timeout']}s for the inner "
              f"session to register (server may still come up). Inspect {logpath}.",
              file=sys.stderr)
        return 1
    print(f"  session: {sid}")

    # Tag the subsession's title with [SUB] (preserved across watcher re-renders
    # via session_titles.extract_sub_token). Best-effort: a failure here logs
    # and continues -- the spawn + prompt itself is what matters.
    if subname:
        _post_subname_title(sid, cwd, cfg.host, subname, cfg.dev, log,
                            task=opts.get("task") or "",
                            set_title=set_title, get_token=get_token)

    if prompt_body is None:
        return 0

    if reply_to:
        from .session_list import format_reply_header
        prompt_body = format_reply_header(reply_to, prompt_body)

    return _submit_prompt(sid, prompt_body, log,
                          submit=submit, get_token=get_token)


if __name__ == "__main__":
    raise SystemExit(main())
