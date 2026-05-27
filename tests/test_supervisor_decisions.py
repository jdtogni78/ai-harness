import tempfile
import unittest
from pathlib import Path

from remote_control.config import SupervisorConfig
from remote_control.supervisor import (
    Supervisor,
    is_busy,
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
    def _make(self, allow="AppOne\napp-two\n", idle_recycle=43200, host="mm"):
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


if __name__ == "__main__":
    unittest.main()
