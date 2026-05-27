import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_control import new_session
from remote_control.new_session import autogen_name, build_argv, pick_spawn_mode


class AutogenNameTest(unittest.TestCase):
    def test_format_and_no_mm_prefix(self):
        name = autogen_name("mini", rng=lambda n: "deadbeef")
        self.assertEqual(name, "oneoff-mini-deadbeef")
        self.assertFalse(name.startswith("mm-"))

    def test_uses_host_passed_in(self):
        self.assertTrue(autogen_name("note", rng=lambda n: "abc").startswith("oneoff-note-"))


class PickSpawnModeTest(unittest.TestCase):
    def test_worktree_when_git(self):
        self.assertEqual(pick_spawn_mode(Path("/x"), lambda p: True), "worktree")

    def test_same_dir_when_not_git(self):
        self.assertEqual(pick_spawn_mode(Path("/x"), lambda p: False), "same-dir")


class BuildArgvTest(unittest.TestCase):
    def test_capacity_1_and_explicit_create(self):
        argv = build_argv(Path("/bin/claude"), "oneoff-mini-abc", "worktree",
                          "acceptEdits")
        self.assertEqual(argv[:3], ["/bin/claude", "remote-control", "--name"])
        self.assertEqual(argv[3], "oneoff-mini-abc")
        self.assertIn("--capacity", argv)
        self.assertEqual(argv[argv.index("--capacity") + 1], "1")
        self.assertEqual(argv[argv.index("--spawn") + 1], "worktree")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        # We DELIBERATELY do not pass --no-create-session-in-dir (we want the
        # session row visible immediately).
        self.assertNotIn("--no-create-session-in-dir", argv)


class _FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return mock.Mock(pid=7777)


def _env(tmp: str) -> dict:
    """Env that pins host + paths so the test doesn't touch the real machine."""
    return {
        "HOME": tmp,
        "REMOTE_CONTROL_HOST": "mini",
        "REMOTE_CONTROL_DEV": tmp,
        "REMOTE_CONTROL_LOGDIR": tmp,
        "REMOTE_CONTROL_ACTIVE_FILE": str(Path(tmp) / "active-dirs.txt"),
        "REMOTE_CONTROL_CLAUDE_BIN": sys.executable,
        "PATH": "/usr/bin:/bin",
    }


class MainTest(unittest.TestCase):
    def test_dry_run_does_not_spawn(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--dry-run"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "deadbeef")
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])

    def test_missing_dir_errors(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", "/no/such/dir"],
                    popen=fake, git_probe=lambda p: True)
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_rejects_mm_name(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--name", "mm-evil"],
                    popen=fake, git_probe=lambda p: True)
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_rejects_unknown_spawn_mode(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--spawn", "bogus"],
                    popen=fake, git_probe=lambda p: True)
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_launch_spawns_detached(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "deadbeef")
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 1)
        cmd, kw = fake.calls[0]
        # claude binary, remote-control mode, our generated name.
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "remote-control")
        self.assertIn("oneoff-mini-deadbeef", cmd)
        self.assertEqual(cmd[cmd.index("--capacity") + 1], "1")
        self.assertEqual(cmd[cmd.index("--spawn") + 1], "worktree")
        self.assertEqual(kw["cwd"], str(Path(d).resolve()))
        self.assertTrue(kw["start_new_session"])

    def test_auto_spawn_mode_same_dir_when_not_git(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d],
                    popen=fake, git_probe=lambda p: False,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        cmd, _ = fake.calls[0]
        self.assertEqual(cmd[cmd.index("--spawn") + 1], "same-dir")

    def test_explicit_spawn_mode_wins(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--spawn", "same-dir"],
                    popen=fake, git_probe=lambda p: True,  # auto would say worktree
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        cmd, _ = fake.calls[0]
        self.assertEqual(cmd[cmd.index("--spawn") + 1], "same-dir")

    def test_missing_claude_binary_errors(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            env = _env(d)
            env["REMOTE_CONTROL_CLAUDE_BIN"] = "/no/such/claude"
            with mock.patch.dict(os.environ, env, clear=False):
                rc = new_session.main(
                    ["--dir", d],
                    popen=fake, git_probe=lambda p: True)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.calls, [])

    def test_unknown_arg_errors(self):
        rc = new_session.main(
            ["--bogus"], popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
