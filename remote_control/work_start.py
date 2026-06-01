"""``python3 -m remote_control work start`` -- trigger fresh agent work.

Spawn a new agent run from a prompt, on a chosen engine and in a chosen repo:

  * **Codex**  -> ``codex exec "<prompt>"`` (non-interactive).
  * **Claude** -> ``claude -p --permission-mode bypassPermissions "<prompt>"``
    (headless print mode, the same permission posture the supervisor spawns
    servers with -- the global perm-gate hook still vets every tool call).

There is no API to create a *cloud* Claude session from a prompt, so the Claude
side is a local headless run; both engines run on this machine.

Triggering work is outward-facing and hard to undo, so this is **dry-run by
default**: it prints the exact command + cwd and spawns nothing. Pass ``--go`` to
actually launch -- a detached child (its own session/log), mirroring the
usage-limit monitor's Codex resume (:func:`usage_limit.monitor.attempt_resume_codex`).

This is the **trigger** slice of the cross-engine work-orchestration epic
(trigger / inventory / stale-detect / migrate). The pure helpers (command
building, target-dir resolution) live above ``main`` and unit-test against plain
values; the spawn (with an injectable ``popen``) lives in ``main``.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .config import LOGDIR, CodexConfig, SupervisorConfig

ENGINES = ("codex", "claude")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def build_command(engine: str, prompt: str, *, claude_bin, codex_bin) -> List[str]:
    """The argv to spawn for *engine* running *prompt*, or raise ``ValueError``.

    Claude runs headless with ``--permission-mode bypassPermissions`` (the
    supervisor's posture for unattended servers) so a triggered run can act
    without hanging on in-Claude prompts; the host's global perm-gate hook
    (project_perm_gate.md, #23) still vets every tool call."""
    if not prompt.strip():
        raise ValueError("a prompt is required")
    if engine == "codex":
        return [str(codex_bin), "exec", prompt]
    if engine == "claude":
        return [str(claude_bin), "-p", "--permission-mode", "bypassPermissions", prompt]
    raise ValueError(f"--engine must be codex or claude, got {engine!r}")


def resolve_target_dir(repo: Optional[str], explicit_dir: Optional[str],
                       dev_root, cwd: str) -> Path:
    """Where to run: ``--dir`` wins, else ``<dev>/<repo>``, else the cwd."""
    if explicit_dir:
        return Path(explicit_dir)
    if repo:
        return Path(dev_root) / repo
    return Path(cwd)


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control work start --engine codex|claude "
    "[--repo REPO | --dir PATH] [--go] \"<prompt>\"\n"
    "  Spawn a fresh agent run from a prompt. DRY-RUN by default (prints the\n"
    "  command + cwd, spawns nothing); pass --go to actually launch.\n"
    "  --engine : codex (`codex exec`) or claude (headless `claude -p`)\n"
    "  --repo   : run in <dev>/<repo>           --dir : run in an explicit path\n"
    "  --go     : actually spawn the (detached) run\n"
    "  --dev    : dev root for --repo (default ~/dev)"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"engine": None, "repo": None, "dir": None, "go": False,
            "dev": None, "prompt": ""}
    words: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--engine":
            i += 1; opts["engine"] = argv[i]
        elif a == "--repo":
            i += 1; opts["repo"] = argv[i]
        elif a == "--dir":
            i += 1; opts["dir"] = argv[i]
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a == "--go":
            opts["go"] = True
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif not a.startswith("-"):
            words.append(a)
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    opts["prompt"] = " ".join(words)
    return opts


def main(argv: Optional[List[str]] = None, popen=None) -> int:
    popen = subprocess.Popen if popen is None else popen
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0

    sup = SupervisorConfig.from_env()
    codex_cfg = CodexConfig.from_env()
    dev_root = opts["dev"] or str(sup.dev)
    try:
        cmd = build_command(opts["engine"], opts["prompt"],
                            claude_bin=sup.claude_bin, codex_bin=codex_cfg.codex_bin)
    except ValueError as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2

    target = resolve_target_dir(opts["repo"], opts["dir"], dev_root, os.getcwd())
    if not target.is_dir():
        print(f"target dir does not exist: {target}", file=sys.stderr)
        return 2

    shown = shlex.join(cmd)
    if not opts["go"]:
        print("work start: DRY-RUN (pass --go to launch)")
        print(f"  engine : {opts['engine']}")
        print(f"  cwd    : {target}")
        print(f"  command: {shown}")
        return 0

    bin_path = Path(cmd[0])
    if not bin_path.exists():
        print(f"{opts['engine']} binary not found: {bin_path}", file=sys.stderr)
        return 1
    logdir = Path(os.environ.get("REMOTE_CONTROL_LOGDIR", LOGDIR)) / "work-start"
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = logdir / f"{opts['engine']}-{stamp}.log"
    try:
        out = open(out_path, "ab")
    except OSError:
        out = subprocess.DEVNULL
    try:
        proc = popen(cmd, cwd=str(target), stdin=subprocess.DEVNULL,
                     stdout=out, stderr=out, start_new_session=True)
    except (OSError, ValueError) as e:
        print(f"spawn failed: {e}", file=sys.stderr)
        return 1
    finally:
        if hasattr(out, "close"):
            out.close()
    print(f"work start: launched {opts['engine']} (pid {proc.pid})")
    print(f"  cwd    : {target}")
    print(f"  log    : {out_path}")
    print(f"  command: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
