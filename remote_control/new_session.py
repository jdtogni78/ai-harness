"""``python3 -m remote_control new-session`` -- spawn a one-shot
``claude remote-control`` server, picker-visible and supervisor-invisible.

Same shape as the launchd supervisor's per-dir servers, but:

  * ``--capacity 1`` so the server self-exits when its single session ends.
  * Name prefix ``oneoff-`` (not ``mm-`` and not ``<host>-``) so neither
    ``procutil._RUNNING_RE`` nor the supervisor's adopt logic can ever pick
    this server up -- the supervisor's regex scans ``<host>-<basename>`` only.
  * ``--create-session-in-dir`` (the CLI default) is left on, so a session
    row appears in the Claude app picker immediately.

The autogen name is ``oneoff-<nick>-<8hex>``, where ``<nick>`` is this
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
from typing import Any, Callable, List, Mapping, Optional

from .config import SupervisorConfig, UsageLimitConfig
from .procutil import git_usable_worktree, spawn_env


USAGE = (
    "usage: python3 -m remote_control new-session [--dir PATH] [--name SLUG]\n"
    "                                      [--spawn worktree|same-dir]\n"
    "                                      [--permission-mode MODE]\n"
    "                                      [--wait [--wait-timeout SECS]]\n"
    "                                      [--prompt TEXT | --prompt-file PATH]\n"
    "                                      [--reply-to CSE_ID | --no-reply-to]\n"
    "                                      [--subname SLUG | --no-subname]\n"
    "                                      [--dry-run]\n"
    "  Spawn a single-session `claude remote-control` server (capacity 1),\n"
    "  picker-visible, supervisor-invisible. Self-exits when its session ends.\n"
    "    --dir              run in this dir (default: cwd)\n"
    "    --name             server name (default: oneoff-<nick>-<8hex>, where\n"
    "                       <nick> is this machine's host-nick); refuses any\n"
    "                       name starting with 'mm-' or '<host>-' (supervisor\n"
    "                       would otherwise reap or adopt it)\n"
    "    --spawn            worktree|same-dir; default auto from git probe\n"
    "    --permission-mode  passed through (default: acceptEdits, matches supervisor)\n"
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
    "    --subname SLUG     after registration, set the inner session's title\n"
    "                       to `[NICK.host][SLUG] auto-spawned` so the spawned\n"
    "                       subsession is distinguishable from human-driven\n"
    "                       sessions in the picker / session list. The titles\n"
    "                       watcher preserves the [SLUG] tag across re-renders\n"
    "                       (see session_titles.extract_sub_token). Implies\n"
    "                       --wait. Default: auto-derive from the server name\n"
    "                       (strip `oneoff-` prefix); pass --no-subname to skip.\n"
    "    --no-subname       skip setting the [SUB] tag on the inner session\n"
    "    --dry-run          print the command + cwd + log path; don't spawn"
)


# The OSC-8 hyperlink the ``claude`` TUI prints to its stdout (captured into
# the server's log file by our Popen) when its first cloud session connects.
# Same pattern session_titles.build_mm_log_index uses to harvest cse-ids from
# the supervisor's per-server logs; duplicated here so new_session has no
# dependency on session_titles.
_SESSION_LINK_RE = re.compile(rb"session_([A-Za-z0-9]+)\?from=cli")
_LOG_TAIL_BYTES = 64_000


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def autogen_name(host: str, rng: Optional[Callable[[int], str]] = None) -> str:
    """``oneoff-<host>-<8hex>``. *host* is the short host-nick from
    ``config.host_nickname`` (the same value the titles watcher uses to render
    the ``[NICK.host]`` title prefix), so picker rows + log filenames stay
    disambiguated across parallel hosts without re-embedding the full
    hostname."""
    gen = rng or (lambda n: secrets.token_hex(n // 2))
    return f"oneoff-{host}-{gen(8)}"


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
    ``cse_`` prefix that the API exposes. None if absent."""
    m = _SESSION_LINK_RE.search(tail)
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
    its ``oneoff-`` prefix stripped (``oneoff-deadbeef`` -> ``deadbeef``;
    ``oneoff-ff-emails`` -> ``ff-emails``). Returns None if stripping leaves
    nothing usable, in which case the caller skips the title-set step."""
    tail = server_name[len("oneoff-"):] if server_name.startswith("oneoff-") else server_name
    tail = tail.strip()
    return tail or None


def initial_subname_title(
    cwd: Path,
    host: str,
    subname: str,
    dev_root: Path,
    *,
    nicknames_file: Optional[str] = None,
) -> Optional[str]:
    """The ``[NICK.host][SUB] auto-spawned`` title to PUT on a freshly-
    registered subsession, or None if the cwd's repo can't be derived (in
    which case the caller skips the title-set; the watcher will still add a
    bare ``[NICK.host]`` once it can, just without the [SUB] tag).

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
    cwd_s = str(Path(cwd).resolve())
    dev_s = str(Path(dev_root).resolve())
    repo = repo_from_worktree_path(cwd_s) or repo_from_cwd(cwd_s, dev_s)
    if not repo:
        return None
    try:
        file_text = Path(nicknames_file or NICKNAMES_FILE).read_text()
    except OSError:
        file_text = ""
    nmap = build_nickname_map(file_text,
                              os.environ.get("SESSION_TITLE_NICKNAMES", ""))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    # No id yet (the title PUT itself doesn't need it; the ``{id}``/``{shortid}``
    # template tokens get an empty value), host_local=True (we ARE running here).
    vals = session_values({"id": ""}, repo, nmap,
                          host=host, host_local=True, branch="")
    token = render_prefix(template, vals)
    return apply_prefix("auto-spawned", token, sub=subname)


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
        "permission_mode": "acceptEdits", "dry_run": False, "help": False,
        "wait": False, "wait_timeout": 30.0,
        "prompt": None, "prompt_file": None,
        "reply_to": None, "no_reply_to": False,
        "subname": None, "no_subname": False,
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
    title = initial_subname_title(cwd, host, subname, dev_root)
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


def main(argv: Optional[List[str]] = None, popen=None, git_probe=None,
         rng: Optional[Callable[[int], str]] = None,
         env: Optional[Mapping[str, str]] = None,
         waiter: Optional[Callable[..., Optional[str]]] = None,
         submit: Optional[Callable[..., Any]] = None,
         get_token: Optional[Callable[..., Any]] = None,
         set_title: Optional[Callable[..., Any]] = None) -> int:
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

    # A prompt requires the cse_id, so it implies --wait. (--subname also
    # needs the cse_id to PUT the title, but it stays opt-in via --wait/--prompt
    # so the default fire-and-forget spawn doesn't grow a 30s polling loop.)
    want_wait = opts["wait"] or prompt_body is not None

    cfg = SupervisorConfig.from_env(env)
    cwd = Path(opts["dir"] or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"target dir does not exist: {cwd}", file=sys.stderr)
        return 2

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
    # reads from the same SupervisorConfig.state_dir.
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
