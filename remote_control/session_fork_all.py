"""Bulk-fork a list of ``cse_`` sessions, per-id (fork -> verify -> archive),
keeping going past per-id failures so one bad session doesn't strand the rest.

This is the pure composable primitive used by the ``supervisor-restart`` wrapper
script and is usable standalone on any stale set the caller supplies (the
caller decides what "stale" means and feeds in the ids -- see ``sessions list
--stale``). Per-id ordering is ``do_fork`` -> on-disk verify -> (optional)
``archive_session`` so an archive never fires for a session whose fork failed.

Why fork+archive lives here rather than inside ``session_fork``: the single-id
``fork`` command is a deliberately minimal, foot-gun-free tool (just write the
sibling jsonl, no API side effects beyond optional ``--mark``). Bulk mode wants
to chain in archival of the source AND tolerate per-id failure, so the policy
lives separately.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

from .config import DEV, UsageLimitConfig
from .session_fork import default_projects_root, do_fork
from .usage_limit import monitor


class IdResult(NamedTuple):
    cse_id: str
    new_sid: Optional[str]
    fork_ok: bool
    fork_err: str = ""
    archived: Optional[bool] = None  # None = not attempted; True/False = outcome
    archive_err: str = ""


def fork_and_archive_one(
    cse_id: str,
    *,
    projects_root: Path,
    dev: str,
    into_main: bool,
    archive: bool,
    write: bool,
    cfg: UsageLimitConfig,
    token: Optional[str],
    log,
) -> IdResult:
    """One id end-to-end. Fork; verify the sibling is on disk (skipped on dry
    run); then archive the source iff *archive* AND the fork wrote. An archive
    is never attempted when the fork didn't actually write (so dry-run never
    touches the API)."""
    try:
        res = do_fork(cse_id, projects_root=projects_root, dev=dev,
                      into_main=into_main, write=write)
    except (FileNotFoundError, ValueError) as e:
        return IdResult(cse_id, None, False, str(e))
    if write and not res.dest.exists():
        return IdResult(cse_id, res.new_sid, False,
                        f"fork claimed success but {res.dest} missing")
    if not archive or not write:
        return IdResult(cse_id, res.new_sid, True)
    if not token:
        return IdResult(cse_id, res.new_sid, True, archived=False,
                        archive_err="no OAuth token (fork done; archive skipped)")
    code, body = monitor.archive_session(cfg, token, cse_id, log)
    if code == 200:
        return IdResult(cse_id, res.new_sid, True, archived=True)
    return IdResult(cse_id, res.new_sid, True, archived=False,
                    archive_err=f"http={code} body={str(body)[:140]}")


def summarize(results: List[IdResult]) -> str:
    """One-line tally for the trailing summary."""
    n = len(results)
    forks = sum(1 for r in results if r.fork_ok)
    archives = sum(1 for r in results if r.archived is True)
    archive_failed = sum(1 for r in results if r.archived is False)
    parts = [f"{forks}/{n} forked"]
    if any(r.archived is not None for r in results):
        parts.append(f"{archives}/{n} archived")
        if archive_failed:
            parts.append(f"{archive_failed} archive failures")
    return ", ".join(parts)


def all_ok(results: List[IdResult], require_archive: bool) -> bool:
    """``True`` iff every id forked AND (when archive was requested) archived."""
    for r in results:
        if not r.fork_ok:
            return False
        if require_archive and r.archived is not True:
            return False
    return True


USAGE = (
    "usage: python3 -m remote_control fork-all --ids CSE_ID [CSE_ID...]\n"
    "         [--into-main | --no-into-main] [--archive] [--dry-run]\n"
    "         [--dev DIR] [--projects DIR]\n"
    "  --ids        : one or more cse_ ids to bulk-fork (required)\n"
    "  --into-main  : place each sibling in <dev>/<repo> (default ON)\n"
    "  --no-into-main: leave each sibling next to its source instead\n"
    "  --archive    : POST /sessions/{id}/archive after each successful fork\n"
    "  --dry-run    : resolve+report only; no writes, no API calls\n"
    "  exit 0 iff every id forked (and, with --archive, archived)"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {
        "ids": [], "into_main": True, "archive": False, "dry_run": False,
        "dev": DEV, "projects": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ids":
            i += 1
            while i < len(argv) and not argv[i].startswith("-"):
                opts["ids"].append(argv[i]); i += 1
            continue
        if a == "--into-main":
            opts["into_main"] = True
        elif a == "--no-into-main":
            opts["into_main"] = False
        elif a == "--archive":
            opts["archive"] = True
        elif a == "--dry-run":
            opts["dry_run"] = True
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a == "--projects":
            i += 1; opts["projects"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def _report_one(r: IdResult, dry: bool) -> None:
    verb = "would fork" if dry else ("forked" if r.fork_ok else "FAILED fork")
    tail = f" -> {r.new_sid}" if r.new_sid else ""
    print(f"{verb} {r.cse_id}{tail}")
    if not r.fork_ok:
        print(f"  error: {r.fork_err}")
        return
    if r.archived is True:
        print("  archived source")
    elif r.archived is False:
        print(f"  archive FAILED: {r.archive_err}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0
    if not opts["ids"]:
        print(f"fork-all requires --ids CSE_ID [CSE_ID...]\n{USAGE}", file=sys.stderr)
        return 2

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 (API client diagnostics)
    projects = Path(opts["projects"]) if opts["projects"] else default_projects_root()
    cfg = UsageLimitConfig.from_env()
    token = None
    if opts["archive"] and not opts["dry_run"]:
        token = monitor.get_token(cfg, log)
        if not token:
            log("could not read OAuth token; --archive will be skipped per id")

    results: List[IdResult] = []
    for cse_id in opts["ids"]:
        r = fork_and_archive_one(
            cse_id, projects_root=projects, dev=opts["dev"],
            into_main=opts["into_main"], archive=opts["archive"],
            write=not opts["dry_run"], cfg=cfg, token=token, log=log,
        )
        _report_one(r, opts["dry_run"])
        results.append(r)
    print(f"\nsummary: {summarize(results)}")
    return 0 if all_ok(results, opts["archive"] and not opts["dry_run"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
