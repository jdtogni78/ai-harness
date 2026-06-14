import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_control.config import SupervisorConfig
from remote_control import supervisor as supervisor_mod
from remote_control.supervisor import (
    Supervisor,
    is_busy,
    main as supervisor_main,
    should_dispatch_dispatcher,
    should_recycle,
    to_deactivate,
)


class PureDecisionTest(unittest.TestCase):
    def test_is_busy(self):
        self.assertTrue(is_busy(1))
        self.assertTrue(is_busy(5))
        self.assertTrue(is_busy(-1))   # no Capacity line yet -> treat as busy
        self.assertFalse(is_busy(0))

    def test_should_recycle(self):
        self.assertTrue(should_recycle(0, 43200, 43200))   # exactly at threshold
        self.assertTrue(should_recycle(0, 50000, 43200))
        self.assertFalse(should_recycle(0, 43199, 43200))

    def test_to_deactivate(self):
        self.assertEqual(to_deactivate(["mm-a", "mm-b", "mm-c"], {"mm-b"}), ["mm-a", "mm-c"])
        self.assertEqual(to_deactivate([], {"mm-a"}), [])
        self.assertEqual(to_deactivate(["mm-a"], {"mm-a"}), [])

    def test_should_dispatch_dispatcher(self):
        # Happy path: autospawn on, server up, capacity 0, no inflight -> dispatch.
        self.assertTrue(should_dispatch_dispatcher(
            autospawn_enabled=True, dev_server_present=True,
            capacity=0, inflight=False))
        # Disabled.
        self.assertFalse(should_dispatch_dispatcher(
            autospawn_enabled=False, dev_server_present=True,
            capacity=0, inflight=False))
        # No dev server (not allowlisted on this host).
        self.assertFalse(should_dispatch_dispatcher(
            autospawn_enabled=True, dev_server_present=False,
            capacity=0, inflight=False))
        # A session is already attached (human opened one, or our previous
        # dispatch succeeded) -- never double-dispatch.
        self.assertFalse(should_dispatch_dispatcher(
            autospawn_enabled=True, dev_server_present=True,
            capacity=1, inflight=False))
        # Capacity unread (-1 -- log hasn't emitted a Capacity line yet) ->
        # wait. Skipping here is safe because the next tick will retry.
        self.assertFalse(should_dispatch_dispatcher(
            autospawn_enabled=True, dev_server_present=True,
            capacity=-1, inflight=False))
        # A dispatch is still in flight (defense against re-entry; the
        # synchronous default makes this rare but guards against an injected
        # dispatch that returns control early).
        self.assertFalse(should_dispatch_dispatcher(
            autospawn_enabled=True, dev_server_present=True,
            capacity=0, inflight=True))


class FakeProc:
    """In-memory stand-in for procutil: tracks running pids, captures spawns and
    signals. term() models a graceful exit (server dies on TERM)."""

    def __init__(self):
        self.pids = {}        # name -> pid
        self.caps = {}        # name -> capacity (default -1)
        self.spawned = []     # Server list, in order
        self.termed = []      # pids
        self.killed = []      # pids
        self._alive = set()
        self._next = 1000

    def server_pid(self, name):
        return self.pids.get(name)

    def running_servers(self, host):
        # The real procutil.running_servers filters by host prefix; the fake
        # already keys self.pids by server name (which encodes the host), so
        # filter the same way for fidelity.
        prefix = f"{host}-"
        return sorted(n for n in self.pids if n.startswith(prefix))

    def read_capacity(self, logpath):
        return self.caps.get(Path(logpath).stem, -1)

    def git_usable_worktree(self, d):
        return False

    def spawn(self, server, cfg):
        self.spawned.append(server)
        pid = self._next
        self._next += 1
        self.pids[server.name] = pid
        self._alive.add(pid)
        return None  # no Popen object in the fake

    def term(self, pid):
        self.termed.append(pid)
        self._alive.discard(pid)
        for n, p in list(self.pids.items()):
            if p == pid:
                del self.pids[n]

    def kill(self, pid):
        self.killed.append(pid)
        self._alive.discard(pid)

    def alive(self, pid):
        return pid in self._alive


class SupervisorTickTest(unittest.TestCase):
    def _make(self, allow="AppOne\napp-two\n", idle_recycle=43200, host="mm",
              deactivate_min_strikes="1"):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        dev = root / "dev"
        for name in ("AppOne", "app-two", "notallowed"):
            (dev / name).mkdir(parents=True)
        active = root / "active-dirs.txt"
        active.write_text(allow)
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": host,
            "IDLE_RECYCLE_SECS": str(idle_recycle),
            "GRACE_SECS": "1",
            # Existing tests assert one-tick deactivation; keep them on the old
            # zero-hysteresis behaviour. The hysteresis itself gets its own
            # dedicated suite below (DeactivateHysteresisTest).
            "DEACTIVATE_MIN_STRIKES": deactivate_min_strikes,
        })
        self.addCleanup(self.tmp.cleanup)
        proc = FakeProc()
        logs = []
        sup = Supervisor(cfg, proc=proc, log=logs.append, sleep=lambda s: None)
        return sup, proc, logs

    def test_spawns_missing_allowlisted(self):
        sup, proc, _ = self._make()
        sup.tick(now=0)
        self.assertEqual({s.name for s in proc.spawned}, {"mm-AppOne", "mm-app-two"})
        self.assertEqual(set(proc.running_servers("mm")), {"mm-AppOne", "mm-app-two"})

    def test_adopts_running_without_respawn(self):
        sup, proc, _ = self._make()
        proc.pids["mm-AppOne"] = 7777
        proc._alive.add(7777)
        proc.caps["mm-AppOne"] = 0  # idle
        sup.tick(now=100)
        # AppOne adopted (clock starts now, not recycled); app-two spawned.
        self.assertNotIn("mm-AppOne", [s.name for s in proc.spawned])
        self.assertIn("mm-AppOne", sup.last_busy)
        self.assertEqual(sup.last_busy["mm-AppOne"], 100)

    def test_idle_recycle(self):
        sup, proc, logs = self._make(idle_recycle=10)
        proc.pids["mm-AppOne"] = 7777
        proc._alive.add(7777)
        proc.caps["mm-AppOne"] = 0
        sup.last_busy["mm-AppOne"] = 0  # idle since epoch 0
        sup.tick(now=1000)  # 1000 >= 10 -> recycle
        self.assertIn(7777, proc.termed)
        self.assertIn("mm-AppOne", [s.name for s in proc.spawned])
        self.assertTrue(any("recycle: mm-AppOne" in m for m in logs))

    def test_busy_server_not_recycled(self):
        sup, proc, _ = self._make(idle_recycle=10)
        proc.pids["mm-AppOne"] = 7777
        proc._alive.add(7777)
        proc.caps["mm-AppOne"] = 1  # active session
        sup.last_busy["mm-AppOne"] = 0
        sup.tick(now=1000)
        self.assertNotIn(7777, proc.termed)
        self.assertEqual(sup.last_busy["mm-AppOne"], 1000)

    def test_deactivates_unwanted(self):
        sup, proc, logs = self._make(allow="AppOne\n")
        proc.pids["mm-stale"] = 4242   # running but not allowlisted
        proc._alive.add(4242)
        sup.tick(now=0)
        self.assertIn(4242, proc.termed)
        self.assertNotIn("mm-stale", [s.name for s in proc.spawned])
        self.assertTrue(any("deactivate: mm-stale" in m for m in logs))

    def test_host_scoped_spawns_only_on_matching_host(self):
        allow = "AppOne@user\napp-two\n"
        sup, proc, _ = self._make(allow=allow, host="user")
        sup.tick(now=0)
        # Server names carry the host nickname prefix.
        self.assertEqual({s.name for s in proc.spawned}, {"user-AppOne", "user-app-two"})

    def test_host_scoped_skipped_on_other_host(self):
        # AppOne is scoped to user; on "user2" only the bare app-two runs.
        allow = "AppOne@user\napp-two\n"
        sup, proc, _ = self._make(allow=allow, host="user2")
        sup.tick(now=0)
        self.assertEqual({s.name for s in proc.spawned}, {"user2-app-two"})

    def test_missing_active_file_spawns_nothing(self):
        sup, proc, logs = self._make()
        Path(sup.cfg.active_file).unlink()
        sup.tick(now=0)
        self.assertEqual(proc.spawned, [])
        self.assertTrue(any("active-file missing" in m for m in logs))


class ReapExitLoggingTest(unittest.TestCase):
    """Regression for the mini-dev fragility observed in the 2026-06-01
    kickstart test: on supervisor restart, mini-dev's first child exited
    within ~30s and the supervisor respawned it silently on the next tick.
    Without the exit log line the failure leaves no evidence -- and the
    likely cloud-side name-claim race is invisible to root-cause."""

    class _FakeChild:
        """Stub Popen exposing only the surface ``_reap`` touches."""
        def __init__(self, rc):
            self._rc = rc
        def poll(self):
            return self._rc

    def _supervisor_with_child(self, child):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "dev").mkdir()
        active = root / "active-dirs.txt"
        active.write_text("")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(root / "dev"),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": "mm",
        })
        logs = []
        sup = supervisor_mod.Supervisor(
            cfg, proc=FakeProc(), log=logs.append, sleep=lambda s: None,
        )
        sup._children["mm-app"] = child
        return sup, logs

    def test_exited_child_logs_rc_and_is_removed(self):
        sup, logs = self._supervisor_with_child(self._FakeChild(rc=1))
        sup._reap()
        self.assertIn("exit: mm-app rc=1", logs)
        self.assertNotIn("mm-app", sup._children)

    def test_clean_exit_still_logged(self):
        # rc=0 is still worth recording -- the supervisor's perspective is "we
        # asked it to run, it stopped"; whether that was clean or not, the
        # diagnostic log line is what links a respawn to its predecessor.
        sup, logs = self._supervisor_with_child(self._FakeChild(rc=0))
        sup._reap()
        self.assertIn("exit: mm-app rc=0", logs)

    def test_running_child_left_alone(self):
        sup, logs = self._supervisor_with_child(self._FakeChild(rc=None))
        sup._reap()
        self.assertEqual(logs, [])
        self.assertIn("mm-app", sup._children)


class DeactivateHysteresisTest(unittest.TestCase):
    """Regression for ai-harness#8: under launchd, ``discover()`` occasionally
    returned a partial ``wanted`` set on a single tick even though the
    allowlist + filesystem hadn't changed. The old "kill on first miss" rule
    then SIGTERM'd every server the partial set omitted -- which the next tick
    promptly respawned, burning cloud sessions in a 30s loop. With hysteresis
    the supervisor tolerates a single bad tick: a name must be flagged across
    cfg.deactivate_min_strikes consecutive ticks before it gets reaped.
    """

    def _make(self, deactivate_min_strikes=2):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        dev = root / "dev"
        for name in ("AppOne", "app-two"):
            (dev / name).mkdir(parents=True)
        active = root / "active-dirs.txt"
        active.write_text("AppOne\napp-two\n")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": "mm",
            "GRACE_SECS": "1",
            "DEACTIVATE_MIN_STRIKES": str(deactivate_min_strikes),
        })
        self.addCleanup(self.tmp.cleanup)
        proc = FakeProc()
        logs = []
        sup = Supervisor(cfg, proc=proc, log=logs.append, sleep=lambda s: None)
        return sup, proc, logs, dev

    def _flake_discover_once(self, sup, dropped_basenames):
        """Make the next call to ``_discover`` omit *dropped_basenames* (simulating
        the launchd-only partial-wanted bug), then restore normal behaviour."""
        original = sup._discover
        calls = {"n": 0}

        def flaky(allowlist):
            calls["n"] += 1
            if calls["n"] == 1:
                # Mutate the snapshot the supervisor sees: pretend discover()
                # forgot about the listed basenames this tick (the issue #8
                # symptom). Subsequent ticks fall through to the real discover.
                return [s for s in original(allowlist)
                        if s.name.split("-", 1)[1] not in dropped_basenames]
            return original(allowlist)

        sup._discover = flaky  # type: ignore[assignment]

    def test_oscillation_reproducer_without_hysteresis(self):
        # Baseline: with strikes=1 (old behaviour) a single partial-wanted tick
        # is enough to TERM a healthy adopted server. This is the bug.
        sup, proc, logs, _ = self._make(deactivate_min_strikes=1)
        # Both servers already adopted (running pre-tick).
        for name, pid in (("mm-AppOne", 1001), ("mm-app-two", 1002)):
            proc.pids[name] = pid
            proc._alive.add(pid)
        self._flake_discover_once(sup, ["AppOne"])
        sup.tick(now=0)
        # Old behaviour: mm-AppOne killed despite being a legit adopted server.
        self.assertIn(1001, proc.termed,
                      "without hysteresis, a single bad discover() tick reaps "
                      "an adopted server -- this is the oscillation bug")
        self.assertTrue(any("deactivate: mm-AppOne" in m for m in logs))

    def test_single_bad_tick_tolerated_with_hysteresis(self):
        # Same flake under the default strikes=2: the bad tick records a strike
        # but does NOT TERM anything. The next (good) tick clears the strike.
        sup, proc, logs, _ = self._make(deactivate_min_strikes=2)
        for name, pid in (("mm-AppOne", 1001), ("mm-app-two", 1002)):
            proc.pids[name] = pid
            proc._alive.add(pid)
        self._flake_discover_once(sup, ["AppOne"])
        sup.tick(now=0)
        self.assertNotIn(1001, proc.termed,
                         "hysteresis must absorb a single bad discover() tick")
        self.assertEqual(sup._deactivate_strikes, {"mm-AppOne": 1})
        self.assertTrue(any("deactivate-pending: mm-AppOne" in m and "1/2" in m
                            for m in logs))
        # Recovery tick: AppOne back in wanted -> strike cleared, no kill.
        sup.tick(now=30)
        self.assertEqual(sup._deactivate_strikes, {})
        self.assertNotIn(1001, proc.termed)

    def test_repeated_misses_eventually_deactivate(self):
        # A name that's actually unwanted (e.g. user really removed it from
        # active-dirs.txt) must still be reaped -- just after enough strikes.
        sup, proc, logs, _ = self._make(deactivate_min_strikes=2)
        # Pre-running stale server NOT in the allowlist.
        proc.pids["mm-stale"] = 4242
        proc._alive.add(4242)
        sup.tick(now=0)
        self.assertNotIn(4242, proc.termed)        # strike 1: pending only
        self.assertEqual(sup._deactivate_strikes, {"mm-stale": 1})
        sup.tick(now=30)
        self.assertIn(4242, proc.termed)           # strike 2: actually killed
        self.assertNotIn("mm-stale", sup._deactivate_strikes)
        self.assertTrue(any("deactivate-pending: mm-stale" in m for m in logs))
        self.assertTrue(any("deactivate: mm-stale not in allowlist" in m for m in logs))

    def test_strike_resets_when_name_returns(self):
        # Flake tick A: AppOne missing -> strike 1. Tick B: AppOne back -> reset.
        # Flake tick C: AppOne missing AGAIN -> strike 1 (not 2). Must not kill.
        sup, proc, _, _ = self._make(deactivate_min_strikes=2)
        for name, pid in (("mm-AppOne", 1001), ("mm-app-two", 1002)):
            proc.pids[name] = pid
            proc._alive.add(pid)
        self._flake_discover_once(sup, ["AppOne"])
        sup.tick(now=0)
        self.assertEqual(sup._deactivate_strikes.get("mm-AppOne"), 1)
        sup.tick(now=30)  # recovery
        self.assertNotIn("mm-AppOne", sup._deactivate_strikes)
        self._flake_discover_once(sup, ["AppOne"])
        sup.tick(now=60)
        self.assertEqual(sup._deactivate_strikes.get("mm-AppOne"), 1)
        self.assertNotIn(1001, proc.termed)

    def test_strikes_floor_to_one(self):
        # DEACTIVATE_MIN_STRIKES=0 / negative must clamp up to 1, not to 0
        # (which would silently disable deactivation entirely).
        sup, proc, _, _ = self._make(deactivate_min_strikes=0)
        proc.pids["mm-stale"] = 9999
        proc._alive.add(9999)
        sup.tick(now=0)
        # Floored to 1 -> behaves like old code: stale gets killed on tick 1.
        self.assertIn(9999, proc.termed)


class DispatcherAutospawnTest(unittest.TestCase):
    """When `dev` is allowlisted and DISPATCHER_AUTOSPAWN=on, the supervisor's
    tick must dispatch a local-dispatcher cse_ via `new-session` iff the
    `<host>-dev` server has Capacity 0 (no session attached). It must NOT
    dispatch when a session is already attached (Capacity >= 1) and must NOT
    dispatch when autospawn is off."""

    def _make(self, *, autospawn="1", capacity=None, host="mm",
              dispatcher_present=True, allow="dev\n"):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        dev = root / "dev"
        dev.mkdir()
        active = root / "active-dirs.txt"
        active.write_text(allow)
        # Prompt file used by the dispatcher autospawn -- override via env so
        # the supervisor doesn't need the in-repo default path to exist.
        prompt = root / "dispatcher.md"
        if dispatcher_present:
            prompt.write_text("hello, dispatcher\n")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": host,
            "GRACE_SECS": "1",
            "DEACTIVATE_MIN_STRIKES": "1",
            "DISPATCHER_AUTOSPAWN": autospawn,
            "DISPATCHER_PROMPT_FILE": str(prompt),
            "DISPATCHER_WAIT_TIMEOUT_SECS": "5",
        })
        proc = FakeProc()
        # Pre-load capacity if the test wants the dev server to look "already
        # attached" (cap>=1) before the tick.
        if capacity is not None:
            proc.caps[f"{host}-dev"] = capacity
        logs = []
        dispatched = []

        def fake_dispatch(**kw):
            dispatched.append(kw)
            # Model success: the new cse_ holds a slot, so cap flips to 1.
            proc.caps[f"{host}-dev"] = 1
            return True

        sup = Supervisor(
            cfg, proc=proc, log=logs.append, sleep=lambda s: None,
            dispatch=fake_dispatch,
        )
        return sup, proc, logs, dispatched, prompt

    def test_dispatches_when_dev_present_and_idle(self):
        sup, _proc, logs, dispatched, prompt = self._make(capacity=0)
        sup.tick(now=0)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["cwd"], sup.cfg.dev)
        self.assertEqual(dispatched[0]["prompt_file"], prompt)
        self.assertEqual(dispatched[0]["wait_timeout_secs"], 5)
        # The dispatch targets the running <host>-dev server via inject mode.
        self.assertEqual(dispatched[0]["inject_into"], f"{sup.cfg.host}-dev")
        self.assertTrue(any("dispatcher: injecting cse_ into" in m for m in logs))

    def test_does_not_dispatch_when_already_attached(self):
        sup, _proc, _logs, dispatched, _ = self._make(capacity=1)
        sup.tick(now=0)
        self.assertEqual(dispatched, [])

    def test_does_not_dispatch_when_autospawn_off(self):
        sup, _proc, _logs, dispatched, _ = self._make(autospawn="0", capacity=0)
        sup.tick(now=0)
        self.assertEqual(dispatched, [])

    def test_does_not_dispatch_when_dev_not_allowlisted(self):
        # dev not in allowlist -> no <host>-dev server -> nothing to attach to.
        sup, _proc, _logs, dispatched, _ = self._make(allow="", capacity=0)
        sup.tick(now=0)
        self.assertEqual(dispatched, [])

    def test_second_tick_is_noop_after_successful_dispatch(self):
        # The fake dispatch flips capacity to 1; the next tick must observe
        # that and not re-dispatch.
        sup, _proc, _logs, dispatched, _ = self._make(capacity=0)
        sup.tick(now=0)
        sup.tick(now=30)
        self.assertEqual(len(dispatched), 1)

    def test_missing_prompt_file_logs_and_skips(self):
        # Operator misconfigured DISPATCHER_PROMPT_FILE -> log, don't crash,
        # don't shell out. Next tick re-checks (so the operator can fix).
        sup, _proc, logs, dispatched, _ = self._make(
            capacity=0, dispatcher_present=False)
        sup.tick(now=0)
        self.assertEqual(dispatched, [])
        self.assertTrue(any("prompt file missing" in m for m in logs))


class DispatcherFailureBackoffTest(unittest.TestCase):
    """Failure back-off: a FAILED dispatch (archived id / timeout) must NOT
    re-dispatch into the same dev-server process every tick (the slow-loop bug
    from iteration 2). The dev pid is latched on failure too, so a re-dispatch
    only happens once the dev server recycles. A DEAD-SESSION outcome
    (poisoned log) additionally RECYCLES the dev server so a fresh, submittable
    session replaces the preserved/archived one."""

    def _make(self, *, dispatch, host="mm", dev_cap=0):
        from remote_control.supervisor import Supervisor
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dev = root / "dev"; dev.mkdir()
        active = root / "active-dirs.txt"; active.write_text("dev\n")
        prompt = root / "dispatcher.md"; prompt.write_text("go\n")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": host,
            "GRACE_SECS": "1",
            "DISPATCHER_AUTOSPAWN": "1",
            "DISPATCHER_PROMPT_FILE": str(prompt),
        })
        proc = FakeProc()
        dev_name = f"{host}-dev"
        # Pre-place a running dev server with Capacity 0 (no session attached).
        proc.pids[dev_name] = 5000
        proc._alive.add(5000)
        proc.caps[dev_name] = dev_cap
        logs = []
        calls = []

        def wrapped(**kw):
            calls.append(kw)
            return dispatch(len(calls))

        sup = Supervisor(cfg, proc=proc, log=logs.append,
                         sleep=lambda s: None, dispatch=wrapped)
        return sup, proc, logs, calls, dev_name

    def test_generic_failure_latches_pid_no_per_tick_storm(self):
        from remote_control.supervisor import DISPATCH_FAIL
        sup, proc, logs, calls, dev_name = self._make(
            dispatch=lambda n: DISPATCH_FAIL)
        # Three ticks, dev server stays alive at cap 0 the whole time.
        sup._ensure_dispatcher(now=0)
        sup._ensure_dispatcher(now=30)
        sup._ensure_dispatcher(now=60)
        # Exactly ONE dispatch despite three cap==0 ticks -- the failure latched
        # the dev pid, so we don't storm. (Pre-fix: one dispatch per tick.)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sup._dispatched_dev_pid, 5000)
        self.assertTrue(any("latching" in m for m in logs))

    def test_failure_latch_clears_when_dev_server_recycles(self):
        from remote_control.supervisor import DISPATCH_FAIL
        sup, proc, logs, calls, dev_name = self._make(
            dispatch=lambda n: DISPATCH_FAIL)
        sup._ensure_dispatcher(now=0)
        self.assertEqual(len(calls), 1)
        # Simulate the dev server recycling (new pid). The latch must clear so a
        # fresh server run gets a new attempt -- not permanently silenced.
        proc.pids[dev_name] = 7000
        proc._alive.add(7000)
        sup._ensure_dispatcher(now=90)
        self.assertEqual(len(calls), 2)

    def test_dead_session_recycles_dev_server_and_latches(self):
        from remote_control.supervisor import DISPATCH_DEAD_SESSION
        sup, proc, logs, calls, dev_name = self._make(
            dispatch=lambda n: DISPATCH_DEAD_SESSION)
        old_pid = proc.pids[dev_name]
        sup._ensure_dispatcher(now=0)
        self.assertEqual(len(calls), 1)
        # The poisoned dev server was recycled: old pid TERM'd, a fresh server
        # spawned (so the log gets a new run-marker + a fresh session).
        self.assertIn(old_pid, proc.termed)
        self.assertIn(dev_name, [s.name for s in proc.spawned])
        # The new dev server has a different pid -> the latch (set to old pid)
        # won't block the next tick's attempt against the fresh server.
        self.assertNotEqual(proc.pids[dev_name], old_pid)

    def test_no_recycle_storm_dead_then_quiet(self):
        # Dead-session on the first attempt recycles; the SECOND attempt (fresh
        # server) returns generic FAIL (e.g. the app reconnected with no new
        # session link -> no harvestable id). That must latch QUIET, not recycle
        # again -- so a genuinely stuck state can't recycle-loop forever.
        from remote_control.supervisor import (
            DISPATCH_DEAD_SESSION, DISPATCH_FAIL)
        outcomes = {1: DISPATCH_DEAD_SESSION, 2: DISPATCH_FAIL}
        sup, proc, logs, calls, dev_name = self._make(
            dispatch=lambda n: outcomes.get(n, DISPATCH_FAIL))
        sup._ensure_dispatcher(now=0)     # dead -> recycle
        recycles_after_first = len(proc.termed)
        sup._ensure_dispatcher(now=30)    # fresh server, generic fail -> latch
        sup._ensure_dispatcher(now=60)    # latched -> no dispatch, no recycle
        self.assertEqual(len(calls), 2)               # only two attempts total
        self.assertEqual(len(proc.termed), recycles_after_first)  # no 2nd recycle


class DispatcherInjectRunawayRegressionTest(unittest.TestCase):
    """Regression for the dispatcher-autospawn RUNAWAY (one-shot storm).

    The bug: ``_default_dispatch`` shelled out to ``new-session`` whose DEFAULT
    behaviour spawned a brand-new ``oneoff-*`` server (capacity 1). The
    dispatcher cse_ attached to THAT new server -- never to the running
    ``<host>-dev`` server. So ``<host>-dev`` Capacity stayed 0/32, and
    ``should_dispatch_dispatcher`` saw ``cap==0`` every tick and re-dispatched
    forever (a per-tick storm of one-shot servers).

    The old ``DispatcherAutospawnTest`` suite MOCKED ``_dispatch`` (and flipped
    capacity to 1 by hand), so it never exercised the real ``new-session``
    side-effect and PASSED despite the bug. This suite exercises the REAL
    dispatch path -- ``new_session.inject_into_server`` -- against a fake dev
    server log, asserting:

      1. dispatch INJECTS into the running ``<host>-dev`` server (harvests its
         pre-created session id, submits the first turn), raising its Capacity
         to 1, and spawns NO ``oneoff-*`` server; and
      2. the second tick is a no-op (no re-dispatch), both because Capacity is
         now 1 AND because the defense-in-depth pid guard refuses to
         re-dispatch into the same live dev server.

    Hermetic: a temp logdir holds a hand-written ``<host>-dev.log`` carrying a
    ``session_<id>`` link; the API submit / title PUT / token fetch are all
    injected fakes -- no real ``claude`` spawn, no network.
    """

    def _make(self, *, host="mm", dev_cap=0):
        from remote_control import new_session as ns

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        dev = root / "dev"
        dev.mkdir()
        logdir = root / "logs"
        logdir.mkdir()
        active = root / "active-dirs.txt"
        active.write_text("dev\n")
        prompt = root / "dispatcher.md"
        prompt.write_text("you are the dispatcher\n")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(logdir),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": host,
            "GRACE_SECS": "1",
            "DEACTIVATE_MIN_STRIKES": "1",
            "DISPATCHER_AUTOSPAWN": "1",
            "DISPATCHER_PROMPT_FILE": str(prompt),
            "DISPATCHER_WAIT_TIMEOUT_SECS": "5",
        })
        proc = FakeProc()
        dev_name = f"{host}-dev"
        # Model the running dev server: a live pid + a log that already carries
        # the pre-created session's OSC-8 link (what --create-session-in-dir
        # makes the dev server emit), and Capacity 0 (the pre-created session
        # has had no turn yet -- exactly the cap==0 the supervisor gates on).
        proc.pids[dev_name] = 5000
        proc._alive.add(5000)
        proc.caps[dev_name] = dev_cap
        (logdir / f"{dev_name}.log").write_text(
            "Capacity: 0/32\n"
            "https://claude.ai/code/session_DEVDISPATCH?from=cli\n"
        )

        submitted = []
        titled = []

        def fake_submit(_cfg, _token, sid, message, _log):
            submitted.append((sid, message))
            # The submitted turn makes the pre-created session live -> the dev
            # server's Capacity flips to 1 (what the real cloud does).
            proc.caps[dev_name] = 1
            return 200, {"ok": True}

        def fake_set_title(_cfg, _token, _sid, _title):
            titled.append(_title)
            return 200, {"ok": True}

        def fake_get_token(_cfg, _log):
            return "tok"

        # The REAL dispatch path: _default_dispatch normally execs a subprocess;
        # here we route through new_session.inject_into_server in-process (the
        # exact function the subprocess would run) with the API seams faked, so
        # the test exercises the genuine inject logic without a claude spawn.
        def real_inject_dispatch(*, claude_bin, cwd, prompt_file,
                                 wait_timeout_secs, log, inject_into):
            rc = ns.inject_into_server(
                server_name=inject_into, cwd=cwd, cfg=cfg,
                prompt_body=prompt_file.read_text(), subname="dispatcher",
                wait_timeout=wait_timeout_secs, log=log,
                submit=fake_submit, set_title=fake_set_title,
                get_token=fake_get_token,
            )
            return rc == 0

        logs = []
        sup = Supervisor(
            cfg, proc=proc, log=logs.append, sleep=lambda s: None,
            dispatch=real_inject_dispatch,
        )
        return sup, proc, logs, submitted, titled, dev_name

    def test_inject_raises_dev_capacity_and_spawns_no_oneoff(self):
        sup, proc, _logs, submitted, titled, dev_name = self._make(dev_cap=0)
        sup.tick(now=0)
        # The dispatcher turn was submitted into the DEV server's pre-created
        # session id (harvested from its log) -- NOT a fresh oneoff session.
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][0], "cse_DEVDISPATCH")
        self.assertIn("dispatcher", submitted[0][1])
        # Dev server Capacity is now occupied (the whole point of the fix).
        self.assertEqual(proc.caps[dev_name], 1)
        # NO oneoff-* server was started: the only running server is <host>-dev.
        self.assertEqual([n for n in proc.pids if n.startswith("oneoff-")], [])
        self.assertEqual(set(proc.running_servers("mm")), {dev_name})
        # The defense-in-depth pid was recorded against the live dev server.
        self.assertEqual(sup._dispatched_dev_pid, 5000)
        self.assertTrue(any("injecting cse_ into" in m for m in _logs))

    def test_second_tick_is_noop_no_redispatch(self):
        # The runaway symptom: a second tick re-dispatches. After the fix the
        # second tick must NOT submit again -- Capacity is now 1, and even if it
        # weren't, the pid guard blocks a re-dispatch into the same live server.
        sup, proc, _logs, submitted, _titled, _dev = self._make(dev_cap=0)
        sup.tick(now=0)
        sup.tick(now=30)
        self.assertEqual(len(submitted), 1)

    def test_bad_capacity_read_does_not_storm(self):
        # Defense-in-depth (B): even if a later capacity read transiently reports
        # 0 (cloud/log lag) while the SAME dev server process is alive, the pid
        # guard refuses to re-dispatch -- no storm.
        sup, proc, logs, submitted, _titled, dev_name = self._make(dev_cap=0)
        sup.tick(now=0)
        self.assertEqual(len(submitted), 1)
        # Simulate a bad read: capacity drops back to 0 though the dev server
        # (pid 5000) is still the same live process.
        proc.caps[dev_name] = 0
        sup.tick(now=60)
        self.assertEqual(len(submitted), 1)  # still no re-dispatch
        self.assertTrue(any("already dispatched into" in m for m in logs))

    def test_redispatch_after_dev_server_replaced(self):
        # Legitimate re-dispatch: if the dev server is actually recycled (new
        # pid) its pre-created session is gone, so a fresh inject is correct.
        sup, proc, _logs, submitted, _titled, dev_name = self._make(dev_cap=0)
        sup.tick(now=0)
        self.assertEqual(len(submitted), 1)
        # Recycle: new pid, fresh empty-capacity log with a new pre-created sid.
        proc.pids[dev_name] = 6000
        proc._alive.discard(5000)
        proc._alive.add(6000)
        proc.caps[dev_name] = 0
        (sup.cfg.logdir / f"{dev_name}.log").write_text(
            "Capacity: 0/32\n"
            "https://claude.ai/code/session_NEWDISPATCH?from=cli\n"
        )
        sup.tick(now=90)
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[1][0], "cse_NEWDISPATCH")
        self.assertEqual(sup._dispatched_dev_pid, 6000)


class SupervisorMainArgvTest(unittest.TestCase):
    """Regression for: `python -m remote_control supervisor --help` used to
    silently launch a real supervisor because main() ignored argv. That left a
    rogue supervisor running for 9+ hours fighting the launchd-managed one,
    burning cloud registrations (see incident 2026-05-26)."""

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        # Patch Supervisor so we can assert it never runs when argv is rejected.
        with mock.patch.object(supervisor_mod, "Supervisor") as fake_sup:
            fake_sup.return_value.run.return_value = 0
            rc = supervisor_main(argv, stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue(), fake_sup

    def test_help_flag_prints_usage_without_running(self):
        for flag in ("--help", "-h", "help"):
            with self.subTest(flag=flag):
                rc, out, err, fake_sup = self._main([flag])
                self.assertEqual(rc, 0)
                self.assertIn("usage", out.lower())
                self.assertEqual(err, "")
                fake_sup.assert_not_called()

    def test_unknown_arg_errors_without_running(self):
        rc, out, err, fake_sup = self._main(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unexpected argument", err)
        self.assertIn("--bogus", err)
        fake_sup.assert_not_called()

    def test_help_with_extra_args_is_not_help(self):
        # `--help --bogus` shouldn't be silently treated as help — it's an error.
        rc, _out, err, fake_sup = self._main(["--help", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unexpected argument", err)
        fake_sup.assert_not_called()

    def test_no_args_runs_supervisor(self):
        rc, _out, _err, fake_sup = self._main([])
        self.assertEqual(rc, 0)
        fake_sup.assert_called_once()
        fake_sup.return_value.run.assert_called_once()


class SpawnStaggerTest(unittest.TestCase):
    """Cold-start the supervisor with N dirs all missing pids -- without the
    stagger, ``tick()`` fires ``proc.spawn`` N times in the same millisecond
    and the cloud-side server-registration endpoint 429s every request but
    the first (caught in a live restart test as ``rc=1 -> Registration: Rate
    limited`` lines made visible by the new ``_reap`` exit-code log). With
    the stagger we sleep ``cfg.spawn_stagger_secs`` between siblings -- not
    before the first, not at all in steady state.
    """

    def _make(self, stagger_secs, n_dirs=3, host="mm"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dev = root / "dev"
        names = [f"App{i}" for i in range(n_dirs)]
        for name in names:
            (dev / name).mkdir(parents=True)
        active = root / "active-dirs.txt"
        active.write_text("\n".join(names) + "\n")
        cfg = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": str(dev),
            "REMOTE_CONTROL_LOGDIR": str(root / "logs"),
            "REMOTE_CONTROL_ACTIVE_FILE": str(active),
            "REMOTE_CONTROL_HOST": host,
            "GRACE_SECS": "1",
            "DEACTIVATE_MIN_STRIKES": "1",
            "SPAWN_STAGGER_SECS": stagger_secs,
        })
        proc = FakeProc()
        logs: list = []
        sleeps: list = []
        sup = Supervisor(cfg, proc=proc, log=logs.append, sleep=sleeps.append)
        return sup, proc, sleeps

    def test_stagger_inserted_between_siblings_not_before_first(self):
        # 3 missing -> 3 spawns, 2 staggers (between 1<->2 and 2<->3).
        sup, proc, sleeps = self._make("1.5", n_dirs=3)
        sup.tick(now=0)
        self.assertEqual(len(proc.spawned), 3)
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_no_stagger_for_single_spawn(self):
        # 1 missing -> 1 spawn, 0 staggers. The first spawn is never preceded
        # by a sleep: there's no thundering-herd to mitigate when only one
        # request is going out.
        sup, proc, sleeps = self._make("1.0", n_dirs=1)
        sup.tick(now=0)
        self.assertEqual(len(proc.spawned), 1)
        self.assertEqual(sleeps, [])

    def test_steady_state_costs_no_sleep(self):
        # All servers already adopted (pre-existing pids) -> no spawns, so no
        # staggers. The mitigation is zero-cost in the common case.
        sup, proc, sleeps = self._make("1.0", n_dirs=3)
        for i in range(3):
            name = f"mm-App{i}"
            proc.pids[name] = 1000 + i
            proc._alive.add(1000 + i)
            proc.caps[name] = 0  # idle, no recycle since idle_recycle_secs huge
        sup.tick(now=0)
        self.assertEqual(proc.spawned, [])
        self.assertEqual(sleeps, [])

    def test_zero_stagger_disables_sleep(self):
        # Operators with a fast-recovering cloud-side -- or tests that
        # don't want to mock sleep -- can disable the mitigation by setting
        # SPAWN_STAGGER_SECS=0. Spawns still happen; sleep just doesn't.
        sup, proc, sleeps = self._make("0", n_dirs=3)
        sup.tick(now=0)
        self.assertEqual(len(proc.spawned), 3)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
