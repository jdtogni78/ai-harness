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
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

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
    """Running mm-* servers not wanted this tick (removed from allowlist / dir
    gone) -> clean SIGTERM->KILL, no respawn."""
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
        # up as zombies over the supervisor's long life.
        for name in list(self._children):
            child = self._children[name]
            if child.poll() is not None:
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
        for srv in self._discover(allowlist):
            wanted.add(srv.name)
            pid = self.proc.server_pid(srv.name)
            if pid is None:
                self._spawn(srv, now)
                continue
            self.last_busy.setdefault(srv.name, now)  # adopt: clock starts now
            cap = self.proc.read_capacity(self.cfg.logdir / f"{srv.name}.log")
            if is_busy(cap):
                self.last_busy[srv.name] = now
            elif should_recycle(self.last_busy[srv.name], now, self.cfg.idle_recycle_secs):
                self._recycle(srv, pid, now)

        for name in to_deactivate(self.proc.running_servers(), wanted):
            pid = self.proc.server_pid(name)
            if pid is None:
                continue
            self.log(f"deactivate: {name} not in allowlist (pid {pid})")
            self._kill_server(name, pid)

    def shutdown_all(self) -> None:
        self.log("shutdown: forwarding SIGTERM to all servers")
        # TERM every running mm-* server, regardless of allowlist membership: a
        # server started under an older allowlist still deserves clean deregister.
        for name in self.proc.running_servers():
            pid = self.proc.server_pid(name)
            if pid is not None:
                self.proc.term(pid)
        for _ in range(self.cfg.grace_secs):
            if not self.proc.running_servers():
                break
            self._sleep(1)
        self.log("shutdown: done")

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            self._running = False
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def run(self) -> int:
        self.cfg.logdir.mkdir(parents=True, exist_ok=True)
        self._install_signals()
        self.log(f"supervisor: up (pid {os.getpid()}, host={self.cfg.host}, "
                 f"tick={self.cfg.tick_secs}s, "
                 f"idle_recycle={self.cfg.idle_recycle_secs}s, "
                 f"active_file={self.cfg.active_file})")
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


def main(argv: Optional[List[str]] = None) -> int:
    return Supervisor(SupervisorConfig.from_env()).run()
