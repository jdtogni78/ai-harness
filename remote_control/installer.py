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
    "com.user2.claude-remote-control.plist",
    "com.user2.claude-usage-limit-monitor.plist",
]

REPO_DIR = Path(__file__).resolve().parent.parent


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
