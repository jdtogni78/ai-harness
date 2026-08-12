"""Subcommand dispatch for ``python3 -m remote_control <name> [args]``.

Imports of the heavy service modules are deferred into each branch so that a
typo'd subcommand (or ``--help``) never imports, e.g., the urllib API client.
"""
from __future__ import annotations

import sys
from typing import List, Optional

USAGE = "usage: python3 -m remote_control <supervisor|usage-monitor|telegram-bridge|manager|manager-ui|perm-gate|install|codex-import|titles|sessions|work|fork|fork-all|resume|new-session|relaunch|takeover|eval> [args]"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "supervisor":
        from .supervisor import main as run
        return run(rest)
    if cmd == "usage-monitor":
        from .usage_limit.monitor import main as run
        return run(rest)
    if cmd == "telegram-bridge":
        from .telegram.bridge import main as run
        return run(rest)
    if cmd == "manager":
        from .manager import main as run
        return run(rest)
    if cmd == "manager-ui":
        from .manager_ui import main as run
        return run(rest)
    if cmd == "perm-gate":
        from .perm_gate import main as run
        return run(rest)
    if cmd == "install":
        from .installer import main as run
        return run(rest)
    if cmd == "codex-import":
        from .session_port.cli import main as run
        return run(rest)
    if cmd == "titles":
        from .session_titles import main as run
        return run(rest)
    if cmd == "sessions":
        from .session_list import main as run
        return run(rest)
    if cmd == "work":
        from .inventory import main as run
        return run(rest)
    if cmd == "fork":
        from .session_fork import main as run
        return run(rest)
    if cmd == "fork-all":
        from .session_fork_all import main as run
        return run(rest)
    if cmd == "resume":
        from .session_resume import main as run
        return run(rest)
    if cmd == "new-session":
        from .new_session import main as run
        return run(rest)
    if cmd == "relaunch":
        from .relaunch import main as run
        return run(rest)
    if cmd == "takeover":
        from .session_takeover import main as run
        return run(rest)
    if cmd == "eval":
        from .eval import main as run
        return run(rest)
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    print(f"unknown command: {cmd}\n{USAGE}", file=sys.stderr)
    return 2
