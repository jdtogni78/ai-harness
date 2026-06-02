"""Long-lived supervisor for ``claude remote-control`` servers, one per dev dir.

Port of remote-control-supervisor.sh. Runs as a single persistent process under
launchd (KeepAlive, NOT AbandonProcessGroup) so it:
  - respawns a crashed server within TICK_SECS,
  - on logout/reboot receives SIGTERM and forwards it to every server so they
    deregister cleanly from the cloud (reduces post-reboot "ghost sessions"),
  - idle-recycles a server idle (Capacity == 0) for >= IDLE_RECYCLE_SECS, while
    leaving a server with an active session (Capacity >= 1) alone so in-flight
    work is never killed.

Already-running servers (this supervisor's prior incarnation, or detached ones
from the old model) are *adopted* by PID -- never killed at cutover.

The decision helpers (is_busy / should_recycle / to_deactivate) are pure; the
``Supervisor`` class wires them to the process-control seam (``procutil``) and
the logger, and accepts injected ``sleep``/``clock``/``proc`` for tests.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

SUPERVISOR_USAGE = (
    "usage: python3 -m remote_control supervisor\n"
    "       (no arguments; config is from environment variables -- see config.py)"
)

from . import procutil
from .config import SupervisorConfig
from .discovery import Allowlist, Server, discover, load_allowlist
from .logging_util import make_logger


# --------------------------------------------------------------------------- #
# Pure decisions
# --------------------------------------------------------------------------- #
def is_busy(capacity: int) -> bool:
    """Busy = any active session (cap >= 1), or no Capacity line yet (cap == -1:
    don't recycle a server we can't read)."""
    return capacity >= 1 or capacity == -1


def should_recycle(last_busy: float, now: float, idle_recycle_secs: int) -> bool:
    return (now - last_busy) >= idle_recycle_secs


def to_deactivate(running: Iterable[str], wanted: Set[str]) -> List[str]:
    """Running ``<host>-*`` servers not wanted this tick (removed from
    allowlist / dir gone) -> clean SIGTERM->KILL, no respawn."""
    return [name for name in running if name not in wanted]


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #
class Supervisor:
    def __init__(
        self,
        cfg: SupervisorConfig,
        proc=procutil,
        log: Optional[Callable[[str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.proc = proc
        self.log = log if log is not None else make_logger(cfg.manager_log)
        self._sleep = sleep
        self._clock = clock
        self.last_busy: Dict[str, float] = {}   # name -> epoch last seen busy/spawned/adopted
        self._children: Dict[str, "object"] = {}  # name -> Popen we spawned (for reaping)
        # Per-name count of consecutive ticks where the server has been flagged
        # as "not in allowlist". A name is only deactivated once the count hits
        # cfg.deactivate_min_strikes -- the hysteresis that defends against the
        # transient-partial-wanted oscillation in issue #8. Reset whenever the
        # server reappears in this tick's discover() output (so the strike
        # never persists across an unrelated transient miss).
        self._deactivate_strikes: Dict[str, int] = {}
        self._running = True

    # --- discovery wiring (side effects isolated here) ---
    def _allowlist(self) -> Allowlist:
        # Fail-closed: a missing/unreadable file means "nothing allowed", so a
        # deleted/typo'd config never silently means "spawn everything".
        try:
            text = self.cfg.active_file.read_text()
        except OSError:
            self.log(f"active-file missing: {self.cfg.active_file} (no dirs allowed)")
            return {}
        return load_allowlist(text)

    def _subdirs(self) -> List:
        try:
            return sorted(p for p in self.cfg.dev.iterdir() if p.is_dir())
        except OSError:
            return []

    def _discover(self, allowlist: Allowlist) -> List[Server]:
        return discover(self.cfg.dev, allowlist, self._subdirs(),
                        self.proc.git_usable_worktree, self.cfg.host)

    def _reap(self) -> None:
        # Reap any servers we spawned that have since exited, so they don't pile
        # up as zombies over the supervisor's long life. Log the exit code so a
        # short-lived first-start failure (e.g. a cloud-side name-claim race
        # against a just-SIGTERM'd predecessor) leaves diagnostic evidence --
        # the next tick respawns the server, but without this line the failed
        # start would be silent.
        for name in list(self._children):
            child = self._children[name]
            rc = child.poll()
            if rc is not None:
                self.log(f"exit: {name} rc={rc}")
                del self._children[name]

    # --- lifecycle actions ---
    def _spawn(self, srv: Server, now: float) -> None:
        if not srv.directory.is_dir():
            self.log(f"skip: {srv.directory} gone")
            return
        self.log(f"start: {srv.name} ({srv.directory}, spawn={srv.spawn_mode})")
        child = self.proc.spawn(srv, self.cfg)
        if child is not None:
            self._children[srv.name] = child
        self.last_busy[srv.name] = now

    def _recycle(self, srv: Server, pid: int, now: float) -> None:
        self.log(f"recycle: {srv.name} idle >= {self.cfg.idle_recycle_secs}s (pid {pid})")
        self._kill_server(srv.name, pid)
        self._spawn(srv, now)

    def _kill_server(self, name: str, pid: int) -> None:
        # TERM, wait up to GRACE_SECS for graceful deregister, KILL if still up.
        self.proc.term(pid)
        for _ in range(self.cfg.grace_secs):
            if not self.proc.alive(pid):
                break
            self._sleep(1)
        if self.proc.alive(pid):
            self.log(f"deactivate: {name} unresponsive to TERM, KILL")
            self.proc.kill(pid)
        self.last_busy.pop(name, None)
        child = self._children.pop(name, None)
        if child is not None:
            child.poll()  # reap if it was ours

    # --- main loop ---
    def tick(self, now: float) -> None:
        self._reap()
        allowlist = self._allowlist()
        wanted: Set[str] = set()
        spawn_count = 0
        for srv in self._discover(allowlist):
            wanted.add(srv.name)
            pid = self.proc.server_pid(srv.name)
            if pid is None:
                # Stagger consecutive first-starts: a cold supervisor with N
                # supervised dirs would otherwise call proc.spawn N times in the
                # same millisecond, and the cloud-side server-registration
                # endpoint 429s every request but the first (see
                # SupervisorConfig.spawn_stagger_secs). Skip before the first
                # spawn -- the gap only matters between siblings.
                if spawn_count and self.cfg.spawn_stagger_secs > 0:
                    self._sleep(self.cfg.spawn_stagger_secs)
                self._spawn(srv, now)
                spawn_count += 1
                continue
            self.last_busy.setdefault(srv.name, now)  # adopt: clock starts now
            cap = self.proc.read_capacity(self.cfg.logdir / f"{srv.name}.log")
            if is_busy(cap):
                self.last_busy[srv.name] = now
            elif should_recycle(self.last_busy[srv.name], now, self.cfg.idle_recycle_secs):
                self._recycle(srv, pid, now)

        candidates = to_deactivate(self.proc.running_servers(self.cfg.host), wanted)
        # Drop strikes for anything that's no longer a candidate (it's back in
        # wanted, or it's no longer running) -- so a single recovered tick fully
        # clears prior misses, no half-strike state lingering across ticks.
        candidate_set = set(candidates)
        for name in list(self._deactivate_strikes):
            if name not in candidate_set:
                del self._deactivate_strikes[name]
        for name in candidates:
            self._deactivate_strikes[name] = self._deactivate_strikes.get(name, 0) + 1
            strikes = self._deactivate_strikes[name]
            needed = self.cfg.deactivate_min_strikes
            if strikes < needed:
                self.log(f"deactivate-pending: {name} not in allowlist "
                         f"(strike {strikes}/{needed})")
                continue
            pid = self.proc.server_pid(name)
            if pid is None:
                # Already gone -- clear the strike so we don't carry it forward.
                self._deactivate_strikes.pop(name, None)
                continue
            self.log(f"deactivate: {name} not in allowlist (pid {pid})")
            self._kill_server(name, pid)
            self._deactivate_strikes.pop(name, None)

    def shutdown_all(self) -> None:
        self.log("shutdown: forwarding SIGTERM to all servers")
        # TERM every running ``<host>-*`` server, regardless of allowlist
        # membership: a server started under an older allowlist still deserves
        # clean deregister.
        for name in self.proc.running_servers(self.cfg.host):
            pid = self.proc.server_pid(name)
            if pid is not None:
                self.proc.term(pid)
        for _ in range(self.cfg.grace_secs):
            if not self.proc.running_servers(self.cfg.host):
                break
            self._sleep(1)
        self.log("shutdown: done")

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            self._running = False
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _rehydrate_on_startup(self) -> None:
        """Recover orphaned sessions from the previous supervisor incarnation.

        Runs before the first tick. Two passes, in this order:
          1. :func:`rehydrate.sweep_oneoff_checkpoints` -- fork one-off
             transcripts whose process is gone.
          2. :func:`rehydrate.rehydrate_supervisor_orphans` -- fork bridge
             transcripts whose owning server died at the last SIGTERM. The
             freshly-forked uuids feed into the handoff dispatch below.
          3. :func:`handoff.run_handoff_dispatch` -- spawn a brand-new bridge
             server seeded with a brief from each forked orphan, so the user
             gets a "ready to work" picker row instead of a "ready to pick up"
             one. Gated by ``RESUME_ON_RESTART`` (default ``handoff``; set
             ``off`` to keep the rehydrate forks but skip the handoff layer).

        Failures log and continue -- a broken rehydration mustn't prevent the
        supervisor from booting.
        """
        # Imports deferred so a supervisor that never starts (e.g. an arg-parse
        # error in `main`) doesn't pull in the urllib monitor client.
        from . import handoff, rehydrate
        from .config import UsageLimitConfig
        from .session_fork import default_projects_root
        from .usage_limit import monitor

        try:
            projects_root = default_projects_root()
            ulim = UsageLimitConfig.from_env()
            token = monitor.get_token(ulim, self.log)

            def list_sessions() -> Optional[List[dict]]:
                if not token:
                    return None
                return monitor.list_sessions(ulim, token, self.log)

            rehydrate.sweep_oneoff_checkpoints(
                projects_root=projects_root,
                state_dir=self.cfg.state_dir,
                dev=str(self.cfg.dev),
                alive=self.proc.alive,
                now=self._clock(),
                ttl_secs=self.cfg.resume_ttl_hours * 3600,
                log=self.log,
            )
            rh = rehydrate.rehydrate_supervisor_orphans(
                projects_root=projects_root,
                state_dir=self.cfg.state_dir,
                dev=str(self.cfg.dev),
                list_sessions=list_sessions,
                now=self._clock(),
                ttl_secs=self.cfg.resume_ttl_hours * 3600,
                log=self.log,
            )
            if rh.forked:
                from datetime import datetime, timezone
                handoff.run_handoff_dispatch(
                    cfg=self.cfg,
                    forked_cse_ids=rh.forked,
                    env=os.environ,
                    now_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    log=self.log,
                )
        except Exception as e:  # pragma: no cover -- defensive last-resort
            self.log(f"rehydrate: aborted with unhandled error: {type(e).__name__}: {e}")

    def run(self) -> int:
        self.cfg.logdir.mkdir(parents=True, exist_ok=True)
        self._install_signals()
        self.log(f"supervisor: up (pid {os.getpid()}, host={self.cfg.host}, "
                 f"tick={self.cfg.tick_secs}s, "
                 f"idle_recycle={self.cfg.idle_recycle_secs}s, "
                 f"deactivate_min_strikes={self.cfg.deactivate_min_strikes}, "
                 f"active_file={self.cfg.active_file})")
        self._rehydrate_on_startup()
        while self._running:
            self.tick(self._clock())
            # Sleep TICK_SECS in 1s slices so SIGTERM is acted on within ~1s
            # (CPython's time.sleep resumes after the handler rather than
            # aborting), rather than up to a full tick later.
            for _ in range(self.cfg.tick_secs):
                if not self._running:
                    break
                self._sleep(1)
        self.shutdown_all()
        return 0


def main(argv: Optional[List[str]] = None, *,
         stdout=sys.stdout, stderr=sys.stderr) -> int:
    # supervisor takes no positional args -- config is via env vars. Historically
    # this signature silently ignored argv, so `python -m remote_control supervisor
    # --help` launched a real supervisor and left it running for hours fighting
    # the launchd one (see incident 2026-05-26 -- supervisor kill-spree). Parse
    # argv explicitly now.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in ([], None):
        return Supervisor(SupervisorConfig.from_env()).run()
    if argv[0] in ("-h", "--help", "help") and len(argv) == 1:
        print(SUPERVISOR_USAGE, file=stdout)
        return 0
    print(f"supervisor: unexpected argument: {argv[0]!r}\n{SUPERVISOR_USAGE}",
          file=stderr)
    return 2
