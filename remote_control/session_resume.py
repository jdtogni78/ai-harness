"""Surface local fork sessions for the user to pick up.

This is the local-only counterpart to ``fork`` / ``fork-all``: once a sibling
``<uuid>.jsonl`` exists in ``~/.claude/projects/<encoded-cwd>/``, the Claude
desktop app's ``/resume`` picker (scoped to the cwd) and any terminal
``claude -r <uuid>`` invocation will find it. There is no public API to promote
a local uuid into a fresh ``cse_`` (probed: no ``POST /sessions/{id}/messages``
or transcript-import endpoint exists; see ``work_start.py``'s explicit
"no API to create a cloud Claude session from a prompt"), so resume stays a
local operation and this command just makes that easy.

Modes (composable; the script wrapper in ``scripts/supervisor-restart.sh``
calls this after ``fork-all``):

* default (``--print``): print the ready-to-paste ``claude -r`` commands. Zero
  side effects; the user picks which one(s) to actually run.
* ``--open-app``: ``open -a Claude.app <cwd>`` to bring the desktop app
  forward at the right cwd, so the user can pick the fork from /resume.
* ``--open-terminal``: spawn one Terminal.app window per uuid running
  ``claude -r <uuid>`` so each becomes an interactive session immediately.

The two ``--open-*`` modes are not mutually exclusive; print runs in all
modes. Pure helpers (transcript-dir lookup, command rendering) sit above
``main`` so the wrap-up is unit-testable without spawning anything.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from .session_fork import default_projects_root, encode_project_dir, first_cwd


class ResumeTarget(NamedTuple):
    uuid: str
    transcript: Path
    cwd: str


def find_transcript(uuid: str, projects_root: Path) -> Optional[Path]:
    """Newest ``<uuid>.jsonl`` under any project dir, or None. We don't trust
    just one cwd-encoded dir because a fork can land in either the source or
    the relocated dir depending on ``--into-main``."""
    cands = sorted(Path(projects_root).glob(f"*/{uuid}.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def resume_target(uuid: str, projects_root: Path) -> ResumeTarget:
    """Resolve a uuid to (transcript path, cwd to resume from). Raises
    ``FileNotFoundError`` with the uuid in the message so the bulk caller
    can attribute the failure cleanly."""
    t = find_transcript(uuid, projects_root)
    if t is None:
        raise FileNotFoundError(f"no transcript on disk for uuid {uuid!r}")
    cwd = first_cwd(t.read_text().splitlines()) or str(t.parent)
    return ResumeTarget(uuid, t, cwd)


def resume_command(target: ResumeTarget) -> str:
    """The shell command a user would paste to resume the session interactively,
    safely quoted. We always cd first because ``claude -r`` resolves the session
    from the cwd-encoded project dir."""
    return f"cd {shlex.quote(target.cwd)} && claude -r {target.uuid}"


def terminal_open_argv(target: ResumeTarget) -> List[str]:
    """The ``open -na Terminal.app`` argv that spawns one window running the
    resume command. Returned as a list so the spawner stays substitution-free."""
    return ["open", "-na", "Terminal.app",
            "--args", "zsh", "-lc", resume_command(target)]


def app_open_argv(cwd: str) -> List[str]:
    """``open -a Claude.app <cwd>`` -- brings the desktop app forward at the
    right cwd so the user can pick the fork from its /resume picker."""
    return ["open", "-a", "Claude.app", cwd]


USAGE = (
    "usage: python3 -m remote_control resume UUID [UUID...]\n"
    "         [--open-app] [--open-terminal] [--projects DIR]\n"
    "  UUID            : local session uuid (a fork-all sibling, or any\n"
    "                    `<uuid>.jsonl` under ~/.claude/projects)\n"
    "  --open-app      : also `open -a Claude.app <cwd>` for the first uuid's\n"
    "                    cwd (surfaces the desktop /resume picker)\n"
    "  --open-terminal : spawn one Terminal.app window per uuid running\n"
    "                    `claude -r <uuid>` (interactive immediately)\n"
    "  default (no --open-* flag): just print the resume commands"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"uuids": [], "open_app": False, "open_terminal": False, "projects": None}
    for a in argv:
        if a == "--open-app":
            opts["open_app"] = True
        elif a == "--open-terminal":
            opts["open_terminal"] = True
        elif a.startswith("--projects="):
            opts["projects"] = a[len("--projects="):]
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif a.startswith("-"):
            raise ValueError(f"unknown arg: {a}")
        else:
            opts["uuids"].append(a)
    return opts


def _run(
    uuids: List[str],
    *,
    open_app: bool,
    open_terminal: bool,
    projects_root: Path,
    spawn: Callable[[List[str]], int] = lambda a: subprocess.run(a).returncode,
    log,
) -> int:
    """Resolve every uuid, print its resume command, then (if asked) spawn the
    desktop-app / terminal opens. One missing uuid doesn't abort the rest --
    we still print the commands for the resolvable ones."""
    targets: List[ResumeTarget] = []
    for uuid in uuids:
        try:
            t = resume_target(uuid, projects_root)
        except FileNotFoundError as e:
            log(str(e))
            continue
        targets.append(t)
        print(resume_command(t))
    if not targets:
        return 1
    rc = 0
    if open_app:
        rc = spawn(app_open_argv(targets[0].cwd)) or rc
    if open_terminal:
        for t in targets:
            rc = spawn(terminal_open_argv(t)) or rc
    return rc if len(targets) == len(uuids) else 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except ValueError as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0
    if not opts["uuids"]:
        print(f"resume requires at least one UUID\n{USAGE}", file=sys.stderr)
        return 2
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    projects = Path(opts["projects"]) if opts["projects"] else default_projects_root()
    return _run(
        opts["uuids"], open_app=opts["open_app"],
        open_terminal=opts["open_terminal"], projects_root=projects, log=log,
    )


if __name__ == "__main__":
    raise SystemExit(main())
