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
