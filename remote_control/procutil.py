"""Process-control seam: every OS side effect the supervisor needs (ps/pgrep/
git probes, spawning + signalling servers, reading the Capacity line) lives
here so ``supervisor.py`` can be driven with a fake in tests.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .config import SupervisorConfig
from .discovery import Server

# Used-session count N from the latest "Capacity: N/M" the server's TUI printed.
_CAPACITY_RE = re.compile(rb"Capacity: (\d+)/\d+")
_CAPACITY_TAIL_BYTES = 200000


def _running_re(host: str) -> "re.Pattern[str]":
    """Anchor on ``/claude  remote-control  --name  <host>-...`` (same anchor as
    the shell's awk regex) so we don't match tooling that merely mentions a
    server name in argv. Host is regex-escaped."""
    return re.compile(
        r"/claude\s+remote-control\s+--name\s+(" + re.escape(host) + r"-\S+)"
    )


def running_servers(host: str) -> List[str]:
    """Sorted-unique names of currently-running ``<host>-*`` servers.

    Parses ``ps -axo command`` (not ``pgrep -fl``, whose pattern would
    self-match any concurrent process whose argv contains the literal name).
    """
    try:
        out = subprocess.run(["ps", "-axo", "command"],
                             capture_output=True, text=True)
    except OSError:
        return []
    pattern = _running_re(host)
    names = set()
    for line in out.stdout.splitlines():
        m = pattern.search(line)
        if m:
            names.add(m.group(1))
    return sorted(names)


def server_pid(name: str) -> Optional[int]:
    """First PID of the running server for *name*, or None.

    The trailing " --spawn" keeps e.g. ``<host>-app-two`` from matching
    ``<host>-app-two-docker`` (the same disambiguation the shell used).
    """
    try:
        out = subprocess.run(["pgrep", "-f", f"name {name} --spawn"],
                             capture_output=True, text=True)
    except OSError:
        return None
    for tok in out.stdout.split():
        try:
            return int(tok)
        except ValueError:
            continue
    return None


def git_usable_worktree(directory: Path) -> bool:
    """True iff *directory* is inside a git work tree AND HEAD resolves."""
    d = str(directory)

    def ok(args: List[str]) -> bool:
        try:
            r = subprocess.run(["git", "-C", d] + args,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return r.returncode == 0

    return ok(["rev-parse", "--is-inside-work-tree"]) and ok(["rev-parse", "--verify", "HEAD"])


def read_capacity(logpath: Path) -> int:
    """Used-session count from the latest "Capacity: N/M" in the server log.

    Returns N, or -1 if the file or a Capacity line is absent (freshly started).
    Only the last ~200KB is scanned, mirroring the shell's ``tail -c``.
    """
    p = Path(logpath)
    if not p.is_file():
        return -1
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _CAPACITY_TAIL_BYTES))
            data = f.read()
    except OSError:
        return -1
    matches = _CAPACITY_RE.findall(data)
    if not matches:
        return -1
    try:
        return int(matches[-1])
    except ValueError:
        return -1


def spawn_argv(server: Server, cfg: SupervisorConfig) -> List[str]:
    """The ``claude remote-control`` command line for *server* (pure).

    ``bypassPermissions`` skips the in-Claude permission dialog so unattended
    inner sessions (auto-spawned by the manager, worker dispatches) don't hang
    on ask-list ops with no human at the keyboard. The global ``PreToolUse``
    perm-gate hook (project_perm_gate.md, #23) still vets every tool call --
    it's the host's actual safety layer regardless of ``--permission-mode``.
    Attached human-driven inner sessions on these servers also inherit
    ``bypassPermissions``; the gate continues to gate risky ops."""
    return [
        str(cfg.claude_bin), "remote-control",
        "--name", server.name, "--spawn", server.spawn_mode,
        "--permission-mode", "bypassPermissions",
        "--no-create-session-in-dir",
    ]


# Tool dirs that an interactive shell gets from ~/.zshenv but a launchd-spawned
# supervisor does not (launchd hands processes a bare PATH ~ /usr/bin:/bin:...).
# Homebrew tools the agent shells out to — gh, jq — live in /opt/homebrew/bin
# (Apple Silicon) or /usr/local/bin (Intel); claude lives in ~/.local/bin. We
# prepend these so spawned servers, and the agent Bash shells that inherit their
# env, can always find those tools. "$HOME" is expanded from base_env (kept pure
# for testing); the list mirrors the ~/.zshenv prepend on these machines.
_PATH_PREPEND = ("$HOME/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                 "/usr/local/bin")


def _augment_path(path: str, home: Optional[str]) -> str:
    """Prepend _PATH_PREPEND to *path*, dropping entries already present and any
    "$HOME/..." dir when *home* is unknown. Order is preserved; pure."""
    existing = path.split(os.pathsep) if path else []
    seen = set(existing)
    prefix: List[str] = []
    for d in _PATH_PREPEND:
        if d.startswith("$HOME/"):
            if not home:
                continue
            d = home + d[len("$HOME"):]
        if d not in seen:
            prefix.append(d)
            seen.add(d)
    return os.pathsep.join(prefix + existing)


def spawn_env(cfg: SupervisorConfig, base_env: Mapping[str, str]) -> Dict[str, str]:
    """Child env (pure): inherit *base_env* but (1) set the remote-control session
    name prefix to our host nickname, so the app groups this machine's servers
    under the nickname instead of the raw hostname, and (2) add the
    Homebrew/user bin dirs to PATH (see _PATH_PREPEND) so gh/jq/claude resolve
    despite launchd's bare PATH."""
    env = dict(base_env)
    env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"] = cfg.host
    env["PATH"] = _augment_path(env.get("PATH", ""), env.get("HOME"))
    return env


def spawn(server: Server, cfg: SupervisorConfig) -> Optional[subprocess.Popen]:
    """Start a ``claude remote-control`` server, logging to ``<name>.log``.

    The child is left in the supervisor's process group (NOT a new session) so
    launchd's group SIGTERM at logout/reboot reaches it and the supervisor can
    forward TERM for a clean cloud deregister.
    """
    directory = Path(server.directory)
    if not directory.is_dir():
        return None
    logpath = Path(cfg.logdir) / f"{server.name}.log"
    logpath.parent.mkdir(parents=True, exist_ok=True)
    logf = open(logpath, "a")
    try:
        return subprocess.Popen(
            spawn_argv(server, cfg),
            cwd=str(directory),
            env=spawn_env(cfg, os.environ),
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
    finally:
        # The child holds its own dup of the fd; closing here avoids leaking one
        # per spawn in the long-lived supervisor.
        logf.close()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def term(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
