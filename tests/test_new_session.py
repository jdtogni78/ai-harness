import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_control import new_session
from remote_control.new_session import (
    autogen_name, build_argv, default_subname, extract_session_id,
    initial_subname_title, name_is_safe, pick_spawn_mode, read_log_tail,
    wait_for_session_id,
)


class AutogenNameTest(unittest.TestCase):
    def test_format_no_host_segment(self):
        # Host nickname is intentionally NOT baked into the autogen name: the
        # titles watcher already prefixes the inner session's title with
        # `[NICK.host]`, and the log dir is per-machine, so the host segment
        # was redundant.
        name = autogen_name(rng=lambda n: "deadbeef")
        self.assertEqual(name, "oneoff-deadbeef")
        self.assertFalse(name.startswith("mm-"))

    def test_uses_passed_rng(self):
        self.assertEqual(autogen_name(rng=lambda n: "abc"), "oneoff-abc")


class NameIsSafeTest(unittest.TestCase):
    def test_oneoff_ok(self):
        self.assertIsNone(name_is_safe("oneoff-deadbeef", "mini"))

    def test_mm_rejected(self):
        err = name_is_safe("mm-evil", "mini")
        self.assertIsNotNone(err)
        self.assertIn("mm-", err)

    def test_host_prefix_rejected(self):
        # The supervisor's _RUNNING_RE matches `<host>-\S+` -- so an
        # operator-supplied `mini-foo` would be picked up as an orphaned
        # server and reaped on the next tick.
        err = name_is_safe("mini-foo", "mini")
        self.assertIsNotNone(err)
        self.assertIn("mini-", err)

    def test_host_prefix_check_is_host_aware(self):
        # `mini-foo` is safe on a host whose nickname is `note`.
        self.assertIsNone(name_is_safe("mini-foo", "note"))


class PickSpawnModeTest(unittest.TestCase):
    def test_worktree_when_git(self):
        self.assertEqual(pick_spawn_mode(Path("/x"), lambda p: True), "worktree")

    def test_same_dir_when_not_git(self):
        self.assertEqual(pick_spawn_mode(Path("/x"), lambda p: False), "same-dir")


class BuildArgvTest(unittest.TestCase):
    def test_capacity_1_and_explicit_create(self):
        argv = build_argv(Path("/bin/claude"), "oneoff-abc", "worktree",
                          "acceptEdits")
        self.assertEqual(argv[:3], ["/bin/claude", "remote-control", "--name"])
        self.assertEqual(argv[3], "oneoff-abc")
        self.assertIn("--capacity", argv)
        self.assertEqual(argv[argv.index("--capacity") + 1], "1")
        self.assertEqual(argv[argv.index("--spawn") + 1], "worktree")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        # We DELIBERATELY do not pass --no-create-session-in-dir (we want the
        # session row visible immediately).
        self.assertNotIn("--no-create-session-in-dir", argv)


# --- Log polling + session-id extraction ----------------------------------- #

class ExtractSessionIdTest(unittest.TestCase):
    def test_returns_cse_prefixed_id_from_osc8(self):
        tail = b"...some log... session_01ABCxyz?from=cli ...more..."
        self.assertEqual(extract_session_id(tail), "cse_01ABCxyz")

    def test_returns_first_match_only(self):
        tail = b"session_AAA?from=cli ...later... session_BBB?from=cli"
        self.assertEqual(extract_session_id(tail), "cse_AAA")

    def test_no_match(self):
        self.assertIsNone(extract_session_id(b"nothing here"))


class ReadLogTailTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(read_log_tail(Path("/no/such/log")), b"")

    def test_returns_tail_window(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abcd" * 100)
            f.flush()
            try:
                tail = read_log_tail(Path(f.name), tail_bytes=10)
                self.assertEqual(tail, (b"abcd" * 100)[-10:])
            finally:
                os.unlink(f.name)


class _FakeClock:
    """Monotonic-style clock that advances by *step* on every call."""

    def __init__(self, step: float = 1.0):
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.t
        self.t += self.step
        return v


class WaitForSessionIdTest(unittest.TestCase):
    def test_returns_id_on_first_poll(self):
        clock = _FakeClock(step=0.0)
        sid = wait_for_session_id(
            Path("/ignored"), timeout_secs=5,
            sleep=lambda s: None, clock=clock,
            read_tail=lambda p: b"session_01ABC?from=cli")
        self.assertEqual(sid, "cse_01ABC")

    def test_polls_then_finds(self):
        tails = iter([b"", b"", b"session_FOUND?from=cli"])
        sleeps: list = []
        sid = wait_for_session_id(
            Path("/ignored"), timeout_secs=10, poll_secs=0.1,
            sleep=sleeps.append, clock=_FakeClock(step=0.0),
            read_tail=lambda p: next(tails))
        self.assertEqual(sid, "cse_FOUND")
        # Slept twice between the three reads.
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_timeout_returns_none(self):
        # Clock advances 2s per call; timeout 1s -> first miss exceeds deadline.
        sid = wait_for_session_id(
            Path("/ignored"), timeout_secs=1,
            sleep=lambda s: None, clock=_FakeClock(step=2.0),
            read_tail=lambda p: b"")
        self.assertIsNone(sid)


# --- Spawn / main ----------------------------------------------------------- #

class _FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return mock.Mock(pid=7777)


from typing import Optional  # noqa: E402


def _env(tmp: str, extra: Optional[dict] = None) -> dict:
    """Env that pins host + paths so the test doesn't touch the real machine.

    Deliberately overrides ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` to empty so a
    test running inside a real Claude Code session (this is how we develop)
    doesn't auto-detect that session's cse_id as the worker's reply-to."""
    base = {
        "HOME": tmp,
        "REMOTE_CONTROL_HOST": "mini",
        "REMOTE_CONTROL_DEV": tmp,
        "REMOTE_CONTROL_LOGDIR": tmp,
        "REMOTE_CONTROL_ACTIVE_FILE": str(Path(tmp) / "active-dirs.txt"),
        "REMOTE_CONTROL_CLAUDE_BIN": sys.executable,
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "",
    }
    if extra:
        base.update(extra)
    return base


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

    def test_rejects_host_prefix_name(self):
        # `mini-...` would match the supervisor's _RUNNING_RE and get reaped
        # as an orphan on the next tick.
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--name", "mini-foo"],
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
                    ["--dir", d, "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "deadbeef")
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.calls), 1)
        cmd, kw = fake.calls[0]
        # claude binary, remote-control mode, our generated name.
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "remote-control")
        self.assertIn("oneoff-deadbeef", cmd)
        self.assertEqual(cmd[cmd.index("--capacity") + 1], "1")
        self.assertEqual(cmd[cmd.index("--spawn") + 1], "worktree")
        self.assertEqual(kw["cwd"], str(Path(d).resolve()))
        self.assertTrue(kw["start_new_session"])

    def test_auto_spawn_mode_same_dir_when_not_git(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--no-reply-to"],
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
                    ["--dir", d, "--spawn", "same-dir", "--no-reply-to"],
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
                    ["--dir", d, "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.calls, [])

    def test_unknown_arg_errors(self):
        rc = new_session.main(
            ["--bogus"], popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)


class ReplyToEnvTest(unittest.TestCase):
    def test_explicit_reply_to_sets_env_var(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--reply-to", "cse_MANAGER"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertEqual(kw["env"]["REMOTE_CONTROL_REPLY_TO"], "cse_MANAGER")

    def test_no_reply_to_omits_env_var(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertNotIn("REMOTE_CONTROL_REPLY_TO", kw["env"])

    def test_no_sender_detected_does_not_set_env_var(self):
        # No CLAUDE_CODE_SESSION_ACCESS_TOKEN, no --reply-to: warn but
        # don't fail, and don't propagate any reply-to env var.
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertNotIn("REMOTE_CONTROL_REPLY_TO", kw["env"])

    def test_reply_to_and_no_reply_to_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--reply-to", "cse_X", "--no-reply-to"],
                    popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)


class WaitAndPromptTest(unittest.TestCase):
    def test_wait_flag_polls_and_prints_session_id(self):
        fake = _FakePopen()
        # waiter returns the cse_id synchronously
        waited: list = []

        def fake_waiter(logpath, timeout_secs, **_):
            waited.append((logpath, timeout_secs))
            return "cse_WORKER"

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = new_session.main(
                        ["--dir", d, "--wait", "--wait-timeout", "5",
                         "--no-reply-to"],
                        popen=fake, git_probe=lambda p: True,
                        rng=lambda n: "abc", waiter=fake_waiter)
        self.assertEqual(rc, 0)
        self.assertEqual(len(waited), 1)
        _, timeout = waited[0]
        self.assertEqual(timeout, 5.0)
        self.assertIn("session: cse_WORKER", buf.getvalue())

    def test_wait_timeout_returns_nonzero(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--wait", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: None)
        self.assertEqual(rc, 1)

    def test_prompt_implies_wait_and_calls_submit(self):
        fake = _FakePopen()
        submitted: list = []

        def fake_submit(cfg, token, sid, message, log):
            submitted.append((sid, message))
            return (200, {"ok": True})

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt", "do the thing", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    submit=fake_submit,
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        self.assertEqual(submitted, [("cse_WORKER", "do the thing")])

    def test_prompt_prefixed_with_reply_to_header(self):
        fake = _FakePopen()
        submitted: list = []

        def fake_submit(cfg, token, sid, message, log):
            submitted.append(message)
            return (200, {})

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt", "do it",
                     "--reply-to", "cse_MANAGER"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    submit=fake_submit,
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        body = submitted[0]
        self.assertTrue(body.startswith("[from cse_MANAGER"),
                        f"body={body!r}")
        self.assertIn("do it", body)

    def test_prompt_file_read(self):
        fake = _FakePopen()
        submitted: list = []
        with tempfile.TemporaryDirectory() as d:
            promptfile = Path(d) / "prompt.txt"
            promptfile.write_text("from a file\n")
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt-file", str(promptfile),
                     "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    submit=lambda c, t, sid, msg, log: (submitted.append(msg) or (200, {})),
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        self.assertEqual(submitted, ["from a file\n"])

    def test_prompt_and_prompt_file_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt", "x", "--prompt-file", "/tmp/y"],
                    popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)

    def test_empty_prompt_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt", "   ", "--no-reply-to"],
                    popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)

    def test_submit_failure_returns_nonzero(self):
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--prompt", "x", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    submit=lambda c, t, sid, msg, log: (500, {"err": "x"}),
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 1)

    def test_dry_run_shows_wait_and_prompt_lines(self):  # noqa: E501  (test helper anchor)
        pass

    def test_dry_run_shows_subname_line(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = new_session.main(
                        ["--dir", d, "--dry-run", "--wait",
                         "--subname", "ff-emails"],
                        popen=_FakePopen(), git_probe=lambda p: True,
                        rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("subname: ff-emails", out)


class SubnameTest(unittest.TestCase):
    def test_default_subname_strips_oneoff_prefix(self):
        self.assertEqual(default_subname("oneoff-deadbeef"), "deadbeef")
        self.assertEqual(default_subname("oneoff-ff-emails"), "ff-emails")

    def test_default_subname_unchanged_when_no_oneoff_prefix(self):
        self.assertEqual(default_subname("custom-name"), "custom-name")

    def test_default_subname_empty_returns_none(self):
        self.assertIsNone(default_subname("oneoff-"))

    def test_initial_subname_title_under_dev_root(self):
        # cwd under a "fake-repo" inside dev_root -> repo derived -> title set
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d)
            repo_dir = dev / "fake-repo"
            repo_dir.mkdir()
            title = initial_subname_title(
                repo_dir, host="mini", subname="deadbeef", dev_root=dev,
                nicknames_file="/no/such/file")  # empty file -> derive nickname
            self.assertIsNotNone(title)
            # `fake-repo` -> derived nickname FR (multi-word initials).
            self.assertIn("[FR.mini][deadbeef]", title)
            self.assertIn("auto-spawned", title)

    def test_initial_subname_title_returns_none_when_repo_unresolvable(self):
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d) / "dev"
            dev.mkdir()
            elsewhere = Path(d) / "elsewhere"
            elsewhere.mkdir()
            self.assertIsNone(initial_subname_title(
                elsewhere, host="mini", subname="x", dev_root=dev,
                nicknameS_file="/no/such/file") if False else
                initial_subname_title(elsewhere, host="mini", subname="x",
                                      dev_root=dev,
                                      nicknames_file="/no/such/file"))

    def test_no_subname_skips_title_set(self):
        # With --no-subname, no set_title call should ever be attempted.
        fake = _FakePopen()
        called: list = []
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--wait", "--no-subname", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    set_title=lambda *a, **kw: (called.append(a) or (200, {})))
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])

    def test_subname_calls_set_title_when_repo_derivable(self):
        # Spawn cwd is under dev_root/<repo> -> repo resolves -> set_title fires.
        fake = _FakePopen()
        captured: dict = {}

        def fake_set_title(cfg, token, sid, title):
            captured["sid"], captured["title"] = sid, title
            return (200, {})

        with tempfile.TemporaryDirectory() as d:
            dev = Path(d)
            repo_dir = dev / "fake-repo"
            repo_dir.mkdir()
            env = _env(d, extra={"REMOTE_CONTROL_DEV": str(dev)})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = new_session.main(
                    ["--dir", str(repo_dir), "--wait", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    set_title=fake_set_title,
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        self.assertEqual(captured["sid"], "cse_WORKER")
        # Auto-derived from `oneoff-abc` -> subname "abc"
        self.assertIn("[FR.mini][abc]", captured["title"])

    def test_subname_skipped_silently_when_repo_unresolvable(self):
        # Spawn in a tempdir NOT under dev root -> no repo -> skip set_title,
        # but main still succeeds.
        fake = _FakePopen()
        called: list = []
        with tempfile.TemporaryDirectory() as d:
            elsewhere = Path(d) / "elsewhere"
            elsewhere.mkdir()
            env = _env(d, extra={"REMOTE_CONTROL_DEV": str(Path(d) / "dev")})
            with mock.patch.dict(os.environ, env, clear=False):
                rc = new_session.main(
                    ["--dir", str(elsewhere), "--wait", "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc",
                    waiter=lambda lp, to, **_: "cse_WORKER",
                    set_title=lambda *a, **kw: (called.append(a) or (200, {})),
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        self.assertEqual(called, [])

    def test_subname_and_no_subname_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                rc = new_session.main(
                    ["--dir", d, "--subname", "x", "--no-subname"],
                    popen=_FakePopen(), git_probe=lambda p: True)
        self.assertEqual(rc, 2)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    rc = new_session.main(
                        ["--dir", d, "--dry-run",
                         "--prompt", "hello",
                         "--reply-to", "cse_X"],
                        popen=_FakePopen(), git_probe=lambda p: True,
                        rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("DRY-RUN", out)
        self.assertIn("reply-to: cse_X", out)
        self.assertIn("wait   :", out)
        self.assertIn("prompt :", out)


if __name__ == "__main__":
    unittest.main()
