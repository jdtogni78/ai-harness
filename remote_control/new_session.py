"""``python3 -m remote_control new-session`` -- spawn a one-shot
``claude remote-control`` server, picker-visible and supervisor-invisible.

Same shape as the launchd supervisor's per-dir servers, but:

  * ``--capacity 1`` so the server self-exits when its single session ends.
  * Name prefix ``oneoff-`` (not ``mm-``) so ``procutil._RUNNING_RE`` skips
    it -- never reaped on the next supervisor tick, never adopted.
  * ``--create-session-in-dir`` (the CLI default) is left on, so a session
    row appears in the Claude app picker immediately.

Use when an agent wants a fresh picker-visible session for itself or another
agent from inside the current session -- e.g. ``start-work`` after claiming
a ticket. No active-dirs.txt edit, no extra ``mm-*`` server, no orphan.
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from .config import SupervisorConfig
from .procutil import git_usable_worktree, spawn_env


USAGE = (
    "usage: python3 -m remote_control new-session [--dir PATH] [--name SLUG] "
    "[--spawn worktree|same-dir] [--permission-mode MODE] [--dry-run]\n"
    "  Spawn a single-session `claude remote-control` server (capacity 1),\n"
    "  picker-visible, supervisor-invisible. Self-exits when its session ends.\n"
    "    --dir              run in this dir (default: cwd)\n"
    "    --name             server name (default: oneoff-<host>-<8hex>)\n"
    "    --spawn            worktree|same-dir; default auto from git probe\n"
    "    --permission-mode  passed through (default: acceptEdits, matches supervisor)\n"
    "    --dry-run          print the command + cwd + log path; don't spawn"
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def autogen_name(host: str, rng: Optional[Callable[[int], str]] = None) -> str:
    """``oneoff-<host>-<8hex>``. Never starts with ``mm-`` -- the supervisor's
    regex (procutil._RUNNING_RE) only matches ``mm-*``, so this server is
    invisible to its reap/adopt logic."""
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


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
def _parse_args(argv: List[str]) -> dict:
    opts: dict = {
        "dir": None, "name": None, "spawn": None,
        "permission_mode": "acceptEdits", "dry_run": False, "help": False,
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
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def main(argv: Optional[List[str]] = None, popen=None, git_probe=None,
         rng: Optional[Callable[[int], str]] = None,
         env: Optional[Mapping[str, str]] = None) -> int:
    popen = subprocess.Popen if popen is None else popen
    git_probe = git_usable_worktree if git_probe is None else git_probe
    env = os.environ if env is None else env
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts["help"]:
        print(USAGE)
        return 0

    cfg = SupervisorConfig.from_env(env)
    cwd = Path(opts["dir"] or os.getcwd()).resolve()
    if not cwd.is_dir():
        print(f"target dir does not exist: {cwd}", file=sys.stderr)
        return 2

    name = opts["name"] or autogen_name(cfg.host, rng)
    if name.startswith("mm-"):
        # The supervisor's regex only matches mm-*, so an mm- name would let
        # it adopt/reap our one-off based on active-dirs membership. Refuse.
        print(f"name must not start with 'mm-' (supervisor would adopt it): {name}",
              file=sys.stderr)
        return 2

    spawn_mode = opts["spawn"] or pick_spawn_mode(cwd, git_probe)
    if spawn_mode not in ("worktree", "same-dir"):
        print(f"--spawn must be worktree|same-dir, got {spawn_mode!r}",
              file=sys.stderr)
        return 2

    cmd = build_argv(cfg.claude_bin, name, spawn_mode, opts["permission_mode"])
    logpath = Path(cfg.logdir) / f"{name}.log"

    if opts["dry_run"]:
        print("new-session: DRY-RUN (drop --dry-run to launch)")
        print(f"  name   : {name}")
        print(f"  cwd    : {cwd}")
        print(f"  spawn  : {spawn_mode}")
        print(f"  log    : {logpath}")
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
    try:
        proc = popen(
            cmd, cwd=str(cwd),
            env=spawn_env(cfg, env),
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
    print(f"new-session: launched (pid {proc.pid})")
    print(f"  name   : {name}")
    print(f"  cwd    : {cwd}")
    print(f"  spawn  : {spawn_mode}")
    print(f"  log    : {logpath}")
    print(f"  command: {' '.join(cmd)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
