"""Rehydrate orphaned sessions on supervisor startup / one-off process death.

When the supervisor restarts (``launchctl kickstart -k``) or the host reboots,
every ``<host>-*`` server is SIGTERMed -- their ``cse_`` cloud sessions become
disconnected, but their on-disk transcripts in ``~/.claude/projects/`` survive.
``scripts/supervisor-restart.sh`` already does the right thing manually
(snapshot live ids -> kickstart -> fork orphan transcripts to local siblings),
but nothing runs that flow on the unattended paths (reboot, bare kickstart).

This module is the unattended counterpart, intended to be invoked once at
supervisor startup before the first reconcile tick:

  1. Walk ``~/.claude/projects/*bridge-cse-*`` (the bridge worktrees' project
     dirs) and pick out cse_ sessions whose cloud session is no longer alive
     (disconnected from any live process per the code-sessions API, or already
     vanished from it).
  2. Skip orphans whose newest transcript is older than ``RESUME_TTL_HOURS``
     (default 24h) -- a day-old thread is "done", not "interrupted".
  3. Skip orphans that carry an explicit ``.archived`` sidecar -- the user
     intentionally ended that thread.
  4. Skip orphans already rehydrated (the ledger at
     ``~/.ai-harness/rehydrated/<cse_id>.json`` records the produced uuid)
     unless its sibling transcript has since been deleted -- so running the
     supervisor twice in a row doesn't create duplicate siblings.
  5. For each remaining candidate, fork the transcript to a local sibling via
     :func:`session_fork.do_fork` (``--into-main`` placement, mirroring the
     manual flow), then record a marker so ``resume-work`` can surface it.

The spec's "spawn new supervisor server with ``--resume <uuid>``" path is NOT
implemented: ``claude remote-control --help`` carries no ``--resume`` flag
(verified 2026-05-31), so auto-attach on spawn isn't available without an
upstream CLI change. The rehydrate flow stops at "fork + tag" -- the user
picks the sibling out of the ``/resume`` picker, identical to the manual
``scripts/supervisor-restart.sh`` outcome.

Part B (one-off checkpoints) mirrors the same primitive over a different
liveness signal (process alive vs. cloud session connected): see
:func:`sweep_oneoff_checkpoints`.

Pure helpers (no clock, no network, no filesystem) live above the I/O layer so
they unit-test against plain dicts and a fake projects dir.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
)

from .session_fork import cse_id_from_project_dirname, do_fork

# Sidecar that marks "user is done with this thread; don't rehydrate it".
# A file (not a JSONL record) so it's grep-able and trivial to drop in by hand.
ARCHIVED_MARKER_NAME = ".archived"

# Rehydration ledger filename pattern -- one JSON per cse_ id keyed by the cse_
# id (URL-safe). Living per-id (rather than one big map) means concurrent
# rehydrate calls don't race on a shared file.
REHYDRATED_DIRNAME = "rehydrated"
ONEOFFS_DIRNAME = "oneoffs"

DEFAULT_TTL_HOURS = 24


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class OrphanCandidate(NamedTuple):
    """A bridge-cse-* dir whose cloud session looks orphaned (disconnected /
    vanished) and whose transcript is within the TTL window. Carries enough to
    feed :func:`session_fork.do_fork` without re-walking the projects dir."""
    cse_id: str
    project_dir: Path
    source: Path           # newest .jsonl in project_dir (the source transcript)
    mtime: float


def is_orphan_session(session: Optional[dict]) -> bool:
    """A cse_ session is an "orphan" when no live process is currently serving
    it. Without API access we can't tell, so the proxies are:

      - The session is missing from the live list (``session is None``) -- it
        was archived or vacated.
      - ``connection_status == 'disconnected'`` -- the cloud knows about it but
        no server is attached. (After ``shutdown_all`` TERMs every ``<host>-*``,
        this is what every prior session looks like.)
      - ``status == 'archived'`` -- explicitly ended; NOT an orphan (the user
        chose to close it). Returned False so the caller skips rehydration.

    These map directly to the manual-flow snapshot in
    ``scripts/supervisor-restart.sh``: "the supervisor just SIGTERMed every
    server, so every cse_ id whose transcript survived is now disconnected".
    """
    if session is None:
        return True
    if (session.get("status") or "") == "archived":
        return False
    if (session.get("connection_status") or "") == "disconnected":
        return True
    return False


def find_orphan_candidates(
    projects_root: Path,
    live_by_id: Mapping[str, dict],
    *,
    now: float,
    ttl_secs: int,
    is_archived: Callable[[Path], bool],
    is_already_rehydrated: Callable[[str], bool],
) -> List[OrphanCandidate]:
    """Walk *projects_root* for bridge-cse-* dirs and return the orphan
    candidates worth rehydrating.

    The TTL gate (newest transcript mtime within *ttl_secs* of *now*) runs
    FIRST so we don't waste an API lookup or marker read on a 30-day-old
    transcript. *is_archived* and *is_already_rehydrated* are injected so the
    pure helper isn't aware of where markers live on disk.
    """
    candidates: List[OrphanCandidate] = []
    if not projects_root.is_dir():
        return candidates
    for d in sorted(projects_root.glob("*bridge-cse-*")):
        if not d.is_dir():
            continue
        cse_id = cse_id_from_project_dirname(d.name)
        if not cse_id:
            continue
        transcripts = [p for p in d.glob("*.jsonl") if p.is_file()]
        if not transcripts:
            continue
        src = max(transcripts, key=lambda p: p.stat().st_mtime)
        mtime = src.stat().st_mtime
        if (now - mtime) > ttl_secs:
            continue
        if is_archived(d):
            continue
        if is_already_rehydrated(cse_id):
            continue
        if not is_orphan_session(live_by_id.get(cse_id)):
            continue
        candidates.append(OrphanCandidate(cse_id, d, src, mtime))
    return candidates


class OneoffCheckpoint(NamedTuple):
    """A one-off session checkpoint, as written by ``new_session.py`` at spawn.

    ``session_id`` is None until the spawned ``claude remote-control --capacity 1``
    server writes its first transcript line that names a uuid; the rehydrate
    sweep discovers it lexically (newest jsonl under the dir with mtime
    after ``started_at``) rather than requiring the checkpoint to be updated
    mid-life.
    """
    name: str            # server name, e.g. ``local-mini-deadbeef`` (or legacy ``oneoff-mini-deadbeef``)
    dir: str             # the cwd the one-off was spawned in
    pid: int
    started_at: str      # ISO-8601 UTC
    session_id: Optional[str] = None
    path: Optional[Path] = None  # checkpoint file path, set by the loader


def find_dead_oneoffs(
    checkpoints: Iterable[OneoffCheckpoint],
    *,
    alive: Callable[[int], bool],
    now: float,
    ttl_secs: int,
    started_at_to_epoch: Callable[[str], Optional[float]],
) -> Tuple[List[OneoffCheckpoint], List[OneoffCheckpoint]]:
    """Split *checkpoints* into (recoverable, stale) lists.

    A "recoverable" oneoff has a dead PID AND its checkpoint is within the TTL
    window (so the user might still want to pick it up). A "stale" oneoff has a
    dead PID AND its checkpoint is older than the TTL OR its ``started_at`` is
    unparseable -- the sweep just deletes those rather than yanking back an old
    thread. A oneoff whose PID is still alive is neither: it's still running.
    """
    recoverable: List[OneoffCheckpoint] = []
    stale: List[OneoffCheckpoint] = []
    for cp in checkpoints:
        if alive(cp.pid):
            continue
        ts = started_at_to_epoch(cp.started_at)
        if ts is None or (now - ts) > ttl_secs:
            stale.append(cp)
            continue
        recoverable.append(cp)
    return recoverable, stale


# --------------------------------------------------------------------------- #
# I/O wrappers (small + injectable so the pure helpers stay test-driven)
# --------------------------------------------------------------------------- #
def archived_marker_path(project_dir: Path) -> Path:
    return project_dir / ARCHIVED_MARKER_NAME


def is_project_archived(project_dir: Path) -> bool:
    return archived_marker_path(project_dir).exists()


def rehydrated_marker_path(state_dir: Path, cse_id: str) -> Path:
    """Per-cse_id ledger entry. The cse_ ids are URL-safe alphanumerics so they
    safely double as filenames."""
    return state_dir / REHYDRATED_DIRNAME / f"{cse_id}.json"


def read_rehydrated_marker(state_dir: Path, cse_id: str) -> Optional[dict]:
    p = rehydrated_marker_path(state_dir, cse_id)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def is_already_rehydrated(state_dir: Path, projects_root: Path, cse_id: str) -> bool:
    """A cse_ id "counts as rehydrated" if its marker exists AND the produced
    sibling transcript still exists on disk. If the sibling was deleted out from
    under us (a manual cleanup), drop the marker so the next pass re-forks --
    we'd rather create a new sibling than silently never rehydrate."""
    info = read_rehydrated_marker(state_dir, cse_id)
    if not info:
        return False
    sibling = info.get("sibling_path")
    if sibling and Path(sibling).exists():
        return True
    # Marker stale: sibling was deleted. Drop the marker.
    try:
        rehydrated_marker_path(state_dir, cse_id).unlink()
    except OSError:
        pass
    return False


def write_rehydrated_marker(
    state_dir: Path,
    cse_id: str,
    *,
    new_sid: str,
    source_path: Path,
    sibling_path: Path,
    rehydrated_at: str,
    run_dir: str,
) -> None:
    p = rehydrated_marker_path(state_dir, cse_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "cse_id": cse_id,
        "new_sid": new_sid,
        "source_path": str(source_path),
        "sibling_path": str(sibling_path),
        "rehydrated_at": rehydrated_at,
        "run_dir": run_dir,
    }, indent=2))


def oneoff_checkpoint_path(state_dir: Path, name: str) -> Path:
    return state_dir / ONEOFFS_DIRNAME / f"{name}.json"


def write_oneoff_checkpoint(
    state_dir: Path,
    *,
    name: str,
    directory: str,
    pid: int,
    started_at: str,
    session_id: Optional[str] = None,
) -> Path:
    """Persist a one-off spawn so the supervisor sweep can fork it for the
    /resume picker if its process is gone next time around."""
    p = oneoff_checkpoint_path(state_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "name": name,
        "dir": directory,
        "pid": pid,
        "started_at": started_at,
        "session_id": session_id,
    }, indent=2))
    return p


def remove_oneoff_checkpoint(state_dir: Path, name: str) -> bool:
    """Drop a checkpoint on clean exit. Returns True if it was actually there.

    Failure (file vanished, dir unreadable) is silently ignored -- a missing
    checkpoint is exactly the post-condition we wanted."""
    p = oneoff_checkpoint_path(state_dir, name)
    try:
        p.unlink()
        return True
    except (OSError, FileNotFoundError):
        return False


def load_oneoff_checkpoints(state_dir: Path) -> List[OneoffCheckpoint]:
    """Read every checkpoint under ``<state_dir>/oneoffs/*.json``. Bad/empty
    files are skipped (not fatal): one bad checkpoint shouldn't stop the
    sweep from recovering the rest."""
    out: List[OneoffCheckpoint] = []
    d = state_dir / ONEOFFS_DIRNAME
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            obj = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        try:
            out.append(OneoffCheckpoint(
                name=str(obj.get("name") or p.stem),
                dir=str(obj["dir"]),
                pid=int(obj["pid"]),
                started_at=str(obj.get("started_at") or ""),
                session_id=obj.get("session_id"),
                path=p,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _parse_iso8601(ts: str) -> Optional[float]:
    """ISO-8601 -> epoch seconds, tolerant of ``Z`` suffix. None on garbage."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Top-level entrypoints (wired into Supervisor.run)
# --------------------------------------------------------------------------- #
@dataclass
class RehydrateResult:
    """What the rehydrate pass actually did. Returned for log-line generation
    and for tests that want to assert on outcomes rather than parse log lines."""
    candidates: int = 0
    forked: List[str] = None  # cse_ids that produced a new sibling uuid
    skipped_no_token: bool = False
    errors: List[Tuple[str, str]] = None  # (cse_id, message)

    def __post_init__(self) -> None:
        if self.forked is None:
            self.forked = []
        if self.errors is None:
            self.errors = []


def rehydrate_supervisor_orphans(
    *,
    projects_root: Path,
    state_dir: Path,
    dev: str,
    list_sessions: Callable[[], Optional[List[dict]]],
    now: float,
    ttl_secs: int = DEFAULT_TTL_HOURS * 3600,
    log: Callable[[str], None] = lambda _m: None,
) -> RehydrateResult:
    """Find + fork orphan bridge sessions. Pure orchestration: all I/O is
    behind injected functions (``list_sessions``) and a clock value (``now``).

    Returns the count of candidates and the list of cse_ids forked, so the
    caller can log + assert.
    """
    res = RehydrateResult()
    sessions = list_sessions()
    if sessions is None:
        # API failed (no token, network down) -- without it we can't tell which
        # bridge dirs are orphans vs still-served, and falsely marking a live
        # session as orphan would fork over it. Skip rather than risk damage.
        res.skipped_no_token = True
        log("rehydrate: skipped (no live sessions list available); "
            "use scripts/supervisor-restart.sh for manual rehydration")
        return res

    live_by_id: Dict[str, dict] = {
        s.get("id"): s for s in sessions if isinstance(s, dict) and s.get("id")
    }
    candidates = find_orphan_candidates(
        projects_root, live_by_id,
        now=now, ttl_secs=ttl_secs,
        is_archived=is_project_archived,
        is_already_rehydrated=lambda cid: is_already_rehydrated(state_dir, projects_root, cid),
    )
    res.candidates = len(candidates)
    if not candidates:
        log(f"rehydrate: 0 orphan(s) to fork (TTL={ttl_secs}s, live={len(live_by_id)})")
        return res

    log(f"rehydrate: {len(candidates)} orphan(s) to fork (TTL={ttl_secs}s)")
    from datetime import datetime, timezone
    iso_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for cand in candidates:
        try:
            fr = do_fork(cand.cse_id, projects_root=projects_root, dev=dev,
                         into_main=True, write=True)
        except (FileNotFoundError, ValueError) as e:
            res.errors.append((cand.cse_id, str(e)))
            log(f"rehydrate: FAILED {cand.cse_id}: {e}")
            continue
        if not fr.dest.exists():
            res.errors.append((cand.cse_id, f"fork dest missing: {fr.dest}"))
            log(f"rehydrate: FAILED {cand.cse_id}: sibling not written")
            continue
        write_rehydrated_marker(
            state_dir, cand.cse_id,
            new_sid=fr.new_sid, source_path=cand.source,
            sibling_path=fr.dest, rehydrated_at=iso_now, run_dir=fr.run_dir,
        )
        res.forked.append(cand.cse_id)
        log(f"rehydrate: forked {cand.cse_id} -> {fr.new_sid} "
            f"(repo={fr.repo or '?'}, run_dir={fr.run_dir})")
    log(f"rehydrate: done -- {len(res.forked)}/{len(candidates)} forked")
    return res


@dataclass
class OneoffSweepResult:
    recovered: List[str] = None      # checkpoint names that produced a sibling
    deleted_stale: List[str] = None  # checkpoint names dropped as too old
    errors: List[Tuple[str, str]] = None

    def __post_init__(self) -> None:
        if self.recovered is None:
            self.recovered = []
        if self.deleted_stale is None:
            self.deleted_stale = []
        if self.errors is None:
            self.errors = []


def find_oneoff_transcript(
    projects_root: Path, directory: str, started_at_epoch: Optional[float],
) -> Optional[Path]:
    """The one-off's session transcript on disk: a ``*.jsonl`` whose first
    recorded ``cwd`` matches *directory* AND whose file mtime is at-or-after
    ``started_at_epoch`` (so we don't pick up an unrelated older session in the
    same dir). Returns the newest match, or None if none qualify."""
    from .session_fork import encode_project_dir, first_cwd
    proj_dir = projects_root / encode_project_dir(directory)
    if not proj_dir.is_dir():
        return None
    cands: List[Tuple[float, Path]] = []
    for p in proj_dir.glob("*.jsonl"):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if started_at_epoch is not None and mtime < started_at_epoch - 5:
            # 5s grace: filesystem mtimes can lag a hair behind started_at.
            continue
        try:
            with p.open() as fh:
                if first_cwd(fh) != directory:
                    continue
        except OSError:
            continue
        cands.append((mtime, p))
    if not cands:
        return None
    return max(cands, key=lambda t: t[0])[1]


def sweep_oneoff_checkpoints(
    *,
    projects_root: Path,
    state_dir: Path,
    dev: str,
    alive: Callable[[int], bool],
    now: float,
    ttl_secs: int = DEFAULT_TTL_HOURS * 3600,
    log: Callable[[str], None] = lambda _m: None,
) -> OneoffSweepResult:
    """For each oneoff checkpoint whose PID is gone, find its transcript on
    disk and fork it to a local sibling (so the user can pick it up in
    /resume). Stale checkpoints (TTL exceeded, or unparseable timestamp) are
    simply removed -- never resurrected.

    Returns a structured result for tests + log-line generation."""
    res = OneoffSweepResult()
    checkpoints = load_oneoff_checkpoints(state_dir)
    if not checkpoints:
        return res
    recoverable, stale = find_dead_oneoffs(
        checkpoints, alive=alive, now=now, ttl_secs=ttl_secs,
        started_at_to_epoch=_parse_iso8601,
    )
    for cp in stale:
        log(f"oneoff-sweep: dropping stale checkpoint {cp.name} (started_at={cp.started_at!r})")
        if cp.path is not None:
            try:
                cp.path.unlink()
            except OSError:
                pass
        res.deleted_stale.append(cp.name)
    for cp in recoverable:
        started_ts = _parse_iso8601(cp.started_at)
        # Prefer the explicit session_id if the checkpoint was updated
        # mid-life; otherwise discover it via cwd+mtime.
        src: Optional[Path] = None
        if cp.session_id:
            try:
                from .session_fork import find_source_jsonl
                src = find_source_jsonl(cp.session_id, projects_root)
            except FileNotFoundError:
                src = None
        if src is None:
            src = find_oneoff_transcript(projects_root, cp.dir, started_ts)
        if src is None:
            # No transcript -> nothing to fork; the spawn probably died before
            # the first turn was written. Drop the checkpoint so we don't keep
            # poking at it on every supervisor restart.
            log(f"oneoff-sweep: no transcript for {cp.name} (dir={cp.dir}); dropping")
            if cp.path is not None:
                try:
                    cp.path.unlink()
                except OSError:
                    pass
            continue
        try:
            # Fork the discovered uuid (not the cse_ -- one-offs are local-only
            # workers, so their session id is a uuid, not a cse_).
            uuid_sid = src.stem
            fr = do_fork(uuid_sid, projects_root=projects_root, dev=dev,
                         into_main=False, write=True)
        except (FileNotFoundError, ValueError) as e:
            res.errors.append((cp.name, str(e)))
            log(f"oneoff-sweep: FAILED {cp.name}: {e}")
            continue
        if not fr.dest.exists():
            res.errors.append((cp.name, f"fork dest missing: {fr.dest}"))
            log(f"oneoff-sweep: FAILED {cp.name}: sibling not written")
            continue
        log(f"oneoff-sweep: recovered {cp.name} -> {fr.new_sid} (run_dir={fr.run_dir})")
        res.recovered.append(cp.name)
        # Drop the checkpoint -- it has produced its rescue sibling; if the
        # user wants the recovered session running, that's a fresh spawn from
        # the resume picker.
        if cp.path is not None:
            try:
                cp.path.unlink()
            except OSError:
                pass
    return res
