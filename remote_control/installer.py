"""(Re)installs the LaunchAgents in this repo. Port of install.sh.

Plists must live in ~/Library/LaunchAgents (a launchd requirement); this copies
each repo plist there and (re)bootstraps it. Idempotent.

  python3 -m remote_control install            # all known services
  python3 -m remote_control install <label>    # just one (label or plist name)

Command construction is pure (``plan_install`` / ``launchctl_commands``); the
runner is a thin shell around ``shutil.copyfile`` + ``launchctl``.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

# Plists for the whole fleet live here (one repo, synced across machines). Each
# is named com.<user>.<service>, so the default install picks only the ones for
# the current machine's user (see plan_install). codex-relay is intentionally
# absent: that service was retired during the Python port.
AGENTS = [
    "com.user.claude-remote-control.plist",
    "com.user.claude-usage-limit-monitor.plist",
    "com.user.claude-titles-monitor.plist",
    "com.user2.claude-remote-control.plist",
    "com.user2.claude-usage-limit-monitor.plist",
    "com.user2.claude-titles-monitor.plist",
]

REPO_DIR = Path(__file__).resolve().parent.parent

# The supervisor's allowlist used to live in the repo at
# `<repo>/active-dirs.txt` but was moved to a per-user, per-host location at
# `~/.ai-harness/active-dirs.txt` (chmod 600) -- the file lists private app dir
# names and shouldn't be in a public repo, and it differs per host anyway. The
# repo ships `active-dirs.example.txt` (a comment-only sample) which the
# installer copies into place on first install, so a fresh checkout's first
# supervisor boot finds a valid (empty) allowlist rather than fail-closed-with-
# no-file.
ACTIVE_FILE_SAMPLE = REPO_DIR / "active-dirs.example.txt"

# Embedded copy of the sample so a seed still works when the sample file is
# unreachable (packaged/zipped install, partial checkout). The file is the
# source of truth; this string is the fallback.
_ACTIVE_FILE_FALLBACK = """\
# Allowlist for the ai-harness supervisor (per-user, per-host).
#
# One basename per line. Lines starting with `#` and blank lines are ignored.
# The supervisor re-reads this file every TICK_SECS (~30s):
#   - basenames added here spawn a server within one tick,
#   - basenames removed (or commented out) get SIGTERM'd within one tick.
#
# `dev` is the special name for the ~/dev root itself; the spawned server is
# named <host>-dev, where <host> is the supervisor's host nickname.
# Every other entry is a basename under ~/dev (e.g. `your-app` -> <host>-your-app).
#
# HOST SCOPING: this file is per-host now, but the @<nick> suffix still parses
# (useful if you sync this file via dotfiles across machines). Bare names spawn
# on any host. `name@nick1,nick2` allows several. Nickname is REMOTE_CONTROL_HOST
# (set in the plist) or, if unset, derived from the hostname.
#
# Manage with: /remote-control-dirs

# (No entries by default. Add yours below.)
"""


def _read_sample(sample: Path) -> str:
    try:
        return sample.read_text()
    except OSError:
        return _ACTIVE_FILE_FALLBACK


def seed_active_file(path: Path, out=print, sample: Path = ACTIVE_FILE_SAMPLE) -> bool:
    """Create the user-private allowlist file if it doesn't exist yet, by
    copying the in-repo sample (``active-dirs.example.txt``).

    Returns True if the file was just created (so the caller can log it).
    Idempotent: a no-op if the file already exists. Perms: dir 700, file 600
    (the file names private app dirs). The *sample* argument is overridable
    for tests; in normal runs it defaults to the repo's example file."""
    p = Path(path)
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except OSError:
        pass
    p.write_text(_read_sample(sample))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    out(f"Seeded {p} from {sample.name} (chmod 600); edit it to enable dirs.")
    return True


def _host_file_seed_body(host_value: str) -> str:
    """Default body for a freshly-seeded ``~/.ai-harness/host`` file. A short
    header explains that the FILE (not the plist) is the post-install knob,
    followed by the actual nickname on its own line. Pure for unit testing."""
    return (
        "# Host nickname for this machine (Phase B of #32). One line, lower-case.\n"
        "# Read with precedence: REMOTE_CONTROL_HOST env > THIS FILE > hostname-derive.\n"
        "# The installer seeds this from the plist env on first install; afterwards\n"
        "# this file is authoritative -- rename the host by editing it (no plist edit\n"
        "# required). `#`-prefixed lines and blanks are ignored on read.\n"
        f"{host_value}\n"
    )


def seed_host_file(path: Path, host_value: str, out=print) -> bool:
    """Create ``~/.ai-harness/host`` from the supplied *host_value* if the
    file doesn't exist yet. *host_value* is the plist's
    ``REMOTE_CONTROL_HOST`` env value at install time (the operator's chosen
    nickname for this machine).

    Returns True if the file was just created; False if it already exists or
    if *host_value* is empty (no plist override -> nothing to capture, fall
    through to the hostname-derive path at run time). Idempotent. Perms: dir
    700, file 600 (matches ``seed_active_file`` so the entire host-local
    config tree has the same mode)."""
    p = Path(path)
    if p.exists():
        return False
    value = (host_value or "").strip()
    if not value:
        # Nothing to seed (the plist didn't carry REMOTE_CONTROL_HOST on this
        # host -- run-time will derive from the hostname). Stay silent rather
        # than create an empty/comment-only file.
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except OSError:
        pass
    p.write_text(_host_file_seed_body(value))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    out(f"Seeded {p} with host nickname {value!r} (chmod 600); "
        f"edit it (not the plist) to rename this host.")
    return True


def agent_user(label_or_plist: str) -> str:
    """The ``<user>`` token of a ``com.<user>.<service>`` label/plist name
    (``com.user2.claude-remote-control`` -> ``user2``); "" if it doesn't fit
    that shape."""
    name = label_or_plist
    if name.endswith(".plist"):
        name = name[:-len(".plist")]
    parts = name.split(".")
    return parts[1] if len(parts) >= 3 else ""


def plan_install(
    agents: List[str],
    filter_arg: Optional[str] = None,
    current_user: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """(plist_name, label) pairs to install.

    With a *filter_arg*, just the one whose label OR plist name matches (works on
    any machine, an explicit override). With no filter but a *current_user*, only
    the agents owned by that user (so a shared repo never cross-installs another
    machine's plist). With neither, all of *agents*.
    """
    out = []
    for plist_name in agents:
        label = plist_name[:-len(".plist")] if plist_name.endswith(".plist") else plist_name
        if filter_arg is not None:
            if filter_arg != label and filter_arg != plist_name:
                continue
        elif current_user is not None and agent_user(label) != current_user:
            continue
        out.append((plist_name, label))
    return out


def launchctl_commands(uid: int, label: str, dest: str) -> List[Tuple[List[str], bool]]:
    """Ordered (argv, ignore_failure) launchctl steps to (re)bootstrap a label.
    bootout may fail if the agent isn't loaded yet -- that's expected."""
    domain = f"gui/{uid}"
    return [
        (["launchctl", "bootout", domain, dest], True),
        (["launchctl", "bootstrap", domain, dest], False),
        (["launchctl", "enable", f"{domain}/{label}"], False),
    ]


def install_agent(plist_name, label, uid, repo_dir, la_dir, runner=subprocess.run, out=print):
    src = Path(repo_dir) / plist_name
    dest = Path(la_dir) / plist_name
    Path(la_dir).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    for argv, ignore in launchctl_commands(uid, label, str(dest)):
        r = runner(argv, capture_output=True, text=True)
        if r.returncode != 0 and not ignore:
            out(f"  {' '.join(argv)} -> rc={r.returncode} {(r.stderr or '').strip()}")
    out(f"Installed {label}. Status:")
    status = runner(["launchctl", "print", f"gui/{uid}/{label}"],
                    capture_output=True, text=True)
    for line in (status.stdout or "").splitlines():
        if any(k in line for k in ("state", "run interval", "program")):
            out(line)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv or [])
    filter_arg = argv[0] if argv else None
    uid = os.getuid()
    user = getpass.getuser()
    la_dir = Path.home() / "Library" / "LaunchAgents"
    # Seed the host-local config files BEFORE we (re)bootstrap any agent so
    # the supervisor's first tick has both an allowlist (fail-closed otherwise)
    # AND a captured host nickname on disk. Imports are local to avoid a
    # config<->installer cycle at module load.
    from .config import ACTIVE_FILE, HOST_FILE
    seed_active_file(Path(ACTIVE_FILE))
    # Capture the plist's REMOTE_CONTROL_HOST into the host-local config file
    # so it survives a future plist edit and operators can rename a host by
    # editing the file (not the committed plist). No-op if the plist didn't
    # set REMOTE_CONTROL_HOST on this host (we fall through to hostname-derive
    # at run time -- see config.host_nickname).
    seed_host_file(Path(HOST_FILE), os.environ.get("REMOTE_CONTROL_HOST", ""))
    selected = plan_install(AGENTS, filter_arg, current_user=user)
    if not selected:
        if filter_arg is None:
            print(f"no agents for user {user!r}; known: {', '.join(AGENTS)} "
                  f"(pass a label explicitly to install one anyway)")
        else:
            print(f"no agent matches {filter_arg!r}; known: {', '.join(AGENTS)}")
        return 1
    for plist_name, label in selected:
        install_agent(plist_name, label, uid, REPO_DIR, la_dir)
    return 0
