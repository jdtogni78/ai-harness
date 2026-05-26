import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_control import work_start
from remote_control.work_start import build_command, resolve_target_dir


class BuildCommandTest(unittest.TestCase):
    def test_codex(self):
        self.assertEqual(
            build_command("codex", "fix tests", claude_bin="/c", codex_bin="/x"),
            ["/x", "exec", "fix tests"])

    def test_claude_is_headless_accept_edits(self):
        self.assertEqual(
            build_command("claude", "do it", claude_bin="/c", codex_bin="/x"),
            ["/c", "-p", "--permission-mode", "acceptEdits", "do it"])

    def test_unknown_engine(self):
        with self.assertRaises(ValueError):
            build_command("gemini", "x", claude_bin="/c", codex_bin="/x")

    def test_empty_prompt(self):
        with self.assertRaises(ValueError):
            build_command("codex", "   ", claude_bin="/c", codex_bin="/x")


class ResolveTargetDirTest(unittest.TestCase):
    def test_explicit_dir_wins(self):
        self.assertEqual(
            resolve_target_dir("AppOne", "/tmp/here", "/dev", "/cwd"),
            Path("/tmp/here"))

    def test_repo_under_dev(self):
        self.assertEqual(
            resolve_target_dir("AppOne", None, "/dev", "/cwd"),
            Path("/dev/AppOne"))

    def test_falls_back_to_cwd(self):
        self.assertEqual(resolve_target_dir(None, None, "/dev", "/cwd"), Path("/cwd"))


class _FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return mock.Mock(pid=4242)


class MainTest(unittest.TestCase):
    def test_dry_run_does_not_spawn(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            rc = work_start.main(
                ["--engine", "codex", "--dir", d, "build the thing"], popen=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])  # nothing spawned

    def test_missing_dir_errors(self):
        fake = _FakePopen()
        rc = work_start.main(
            ["--engine", "codex", "--dir", "/no/such/dir", "--go", "x"], popen=fake)
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_go_spawns_with_cwd_and_command(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            env = {"REMOTE_CONTROL_LOGDIR": d,
                   "REMOTE_CONTROL_CODEX_BIN": sys.executable}
            with mock.patch.dict(os.environ, env):
                rc = work_start.main(
                    ["--engine", "codex", "--dir", d, "--go", "hello world"],
                    popen=fake)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 1)
        cmd, kw = fake.calls[0]
        self.assertEqual(cmd, [sys.executable, "exec", "hello world"])
        self.assertEqual(kw["cwd"], d)
        self.assertTrue(kw["start_new_session"])

    def test_missing_engine_errors(self):
        rc = work_start.main(["--dir", "/tmp", "no engine"], popen=_FakePopen())
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
