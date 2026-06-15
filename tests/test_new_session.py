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
    initial_subname_title, inject_into_server, name_is_safe, pick_spawn_mode,
    read_log_tail, submit_active_with_retry, wait_for_session_id,
)


class AutogenNameTest(unittest.TestCase):
    def test_format_includes_host_nick(self):
        # `<nick>` keeps picker rows + log filenames disambiguated across
        # parallel hosts. The full hostname stays out -- the short host-nick
        # (config.host_nickname) is the same value the titles watcher uses for
        # the `[NICK.host]` title prefix.
        name = autogen_name("mini", rng=lambda n: "deadbeef")
        self.assertEqual(name, "local-mini-deadbeef")
        self.assertFalse(name.startswith("mm-"))

    def test_uses_passed_rng(self):
        self.assertEqual(autogen_name("note", rng=lambda n: "abc"),
                         "local-note-abc")


class NameIsSafeTest(unittest.TestCase):
    def test_local_ok(self):
        # The current autogen prefix (#92).
        self.assertIsNone(name_is_safe("local-deadbeef", "mini"))

    def test_local_with_nick_segment_ok(self):
        # `local-<nick>-<8hex>` (the autogen shape) must NOT trip the
        # `<host>-` rejection -- the name starts with `local-`, the nick is
        # just an embedded segment for cross-host disambiguation.
        self.assertIsNone(name_is_safe("local-mini-deadbeef", "mini"))
        self.assertIsNone(name_is_safe("local-m5-deadbeef", "m5"))

    def test_legacy_oneoff_still_ok(self):
        # Back-compat (#92): legacy ``oneoff-*`` names still pass the guard --
        # in-flight servers spawned before the rename keep working.
        self.assertIsNone(name_is_safe("oneoff-deadbeef", "mini"))
        self.assertIsNone(name_is_safe("oneoff-mini-deadbeef", "mini"))

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


class ParseArgsTest(unittest.TestCase):
    def test_default_permission_mode_is_bypass(self):
        # Unattended one-offs (manager dispatches, worker reuse) must not hang
        # on in-Claude permission prompts -- the global perm-gate hook (#23)
        # is the host's actual safety layer. Operators can still pass
        # --permission-mode explicitly to override.
        opts = new_session._parse_args([])
        self.assertEqual(opts["permission_mode"], "bypassPermissions")

    def test_explicit_permission_mode_overrides_default(self):
        opts = new_session._parse_args(["--permission-mode", "plan"])
        self.assertEqual(opts["permission_mode"], "plan")


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

    def test_returns_id_from_bare_url_without_from_cli(self):
        # Newer TUI versions emit `claude.ai/code/session_<id>` without the
        # `?from=cli` suffix; the regex must match either format so spawn-wait
        # works across releases (otherwise the supervisor's handoff and the
        # relaunch CLI both stall waiting for a session id that already
        # registered).
        tail = b"Continue coding in https://claude.ai/code/session_01XYZ\nmore"
        self.assertEqual(extract_session_id(tail), "cse_01XYZ")


class ExtractSessionIdRunAnchorTest(unittest.TestCase):
    """Run-anchoring: the persistent append-mode <host>-dev.log accumulates
    ``session_<id>`` links across restarts (stale + archived). procutil writes a
    run-start marker before each (re)spawn; extract_session_id must harvest only
    ids AT OR AFTER the LAST marker so a pre-run (archived) id is ignored in
    favour of the current run's id. This is the fix for the slow-loop where a
    restart reconnected a preserved/archived session and the inject harvested
    that dead id forever."""

    MARK = new_session._RUN_MARKER_PREFIX

    def test_ignores_pre_run_id_in_favour_of_current_run(self):
        # STALE archived id from a prior run, then a marker, then the fresh id.
        tail = (b"session_01StaleArchived?from=cli\n"
                + self.MARK + b"1700000000.42\n"
                + b"session_01FreshActive?from=cli\n")
        self.assertEqual(extract_session_id(tail), "cse_01FreshActive")

    def test_anchors_to_the_last_marker_across_multiple_runs(self):
        # Several runs accumulated; only the id after the FINAL marker counts.
        tail = (b"session_01Run1?from=cli\n"
                + self.MARK + b"a\n" + b"session_01Run2?from=cli\n"
                + self.MARK + b"b\n" + b"session_01Run3?from=cli\n")
        self.assertEqual(extract_session_id(tail), "cse_01Run3")

    def test_no_id_after_marker_returns_none(self):
        # The current run hasn't printed its session link yet (or the app
        # reconnected a preserved session with no NEW link): no post-marker id
        # -> None, so the caller keeps waiting / times out cleanly rather than
        # harvesting the pre-marker archived id.
        tail = (b"session_01StaleArchived?from=cli\n"
                + self.MARK + b"x\n" + b"server starting...\n")
        self.assertIsNone(extract_session_id(tail))

    def test_no_marker_falls_back_to_whole_tail(self):
        # Backward-compat: logs without any marker (older runs, oneoff spawns)
        # search the whole tail, first match -- unchanged behaviour.
        self.assertEqual(
            extract_session_id(b"x session_01Bare?from=cli y"), "cse_01Bare")


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
        # Autogen name embeds the host-nick (env REMOTE_CONTROL_HOST=mini).
        self.assertIn("local-mini-deadbeef", cmd)
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

    # The next three lock down the #38 fix: even when the PARENT process
    # already has REMOTE_CONTROL_REPLY_TO in its env (the manager-pattern
    # bridge case -- the parent is itself a worker whose own manager
    # injected the var), the child env passed to popen must NOT carry the
    # inherited value unless the explicit --reply-to path re-sets it. We
    # explicitly seed REMOTE_CONTROL_REPLY_TO into the patched os.environ
    # so the test surface matches what was happening at run time inside a
    # real bridge process.
    def test_no_reply_to_strips_inherited_env_var(self):
        polluted = dict(_env(""))
        polluted["REMOTE_CONTROL_REPLY_TO"] = "cse_INHERITED_FROM_PARENT"
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            polluted["HOME"] = d
            polluted["REMOTE_CONTROL_DEV"] = d
            polluted["REMOTE_CONTROL_LOGDIR"] = d
            polluted["REMOTE_CONTROL_ACTIVE_FILE"] = str(Path(d) / "active-dirs.txt")
            with mock.patch.dict(os.environ, polluted, clear=False):
                rc = new_session.main(
                    ["--dir", d, "--no-reply-to"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertNotIn("REMOTE_CONTROL_REPLY_TO", kw["env"])

    def test_no_sender_strips_inherited_env_var(self):
        # No --reply-to AND no CLAUDE_CODE_SESSION_ACCESS_TOKEN to auto-detect
        # from (cleared by _env). Inherited REMOTE_CONTROL_REPLY_TO must
        # still be stripped from the child.
        polluted = dict(_env(""))
        polluted["REMOTE_CONTROL_REPLY_TO"] = "cse_INHERITED_FROM_PARENT"
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            polluted["HOME"] = d
            polluted["REMOTE_CONTROL_DEV"] = d
            polluted["REMOTE_CONTROL_LOGDIR"] = d
            polluted["REMOTE_CONTROL_ACTIVE_FILE"] = str(Path(d) / "active-dirs.txt")
            with mock.patch.dict(os.environ, polluted, clear=False):
                rc = new_session.main(
                    ["--dir", d],  # no flag at all
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertNotIn("REMOTE_CONTROL_REPLY_TO", kw["env"])

    def test_explicit_reply_to_overwrites_inherited_env_var(self):
        # --reply-to <X> while the parent has REMOTE_CONTROL_REPLY_TO=<Y>:
        # the child env must end up with X (the explicit one), not Y
        # (the inherited one). Locks down "explicit wins" over inheritance.
        polluted = dict(_env(""))
        polluted["REMOTE_CONTROL_REPLY_TO"] = "cse_INHERITED_FROM_PARENT"
        fake = _FakePopen()
        with tempfile.TemporaryDirectory() as d:
            polluted["HOME"] = d
            polluted["REMOTE_CONTROL_DEV"] = d
            polluted["REMOTE_CONTROL_LOGDIR"] = d
            polluted["REMOTE_CONTROL_ACTIVE_FILE"] = str(Path(d) / "active-dirs.txt")
            with mock.patch.dict(os.environ, polluted, clear=False):
                rc = new_session.main(
                    ["--dir", d, "--reply-to", "cse_EXPLICIT"],
                    popen=fake, git_probe=lambda p: True,
                    rng=lambda n: "abc")
        self.assertEqual(rc, 0)
        _, kw = fake.calls[0]
        self.assertEqual(kw["env"]["REMOTE_CONTROL_REPLY_TO"], "cse_EXPLICIT")


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
    def test_default_subname_strips_local_prefix(self):
        # Current autogen prefix (#92).
        self.assertEqual(default_subname("local-deadbeef"), "deadbeef")
        self.assertEqual(default_subname("local-ff-emails"), "ff-emails")

    def test_default_subname_strips_legacy_oneoff_prefix(self):
        # Back-compat (#92): legacy ``oneoff-*`` server names still get their
        # prefix stripped, so subsession tags from in-flight legacy servers
        # remain stable across the rename.
        self.assertEqual(default_subname("oneoff-deadbeef"), "deadbeef")
        self.assertEqual(default_subname("oneoff-ff-emails"), "ff-emails")

    def test_default_subname_unchanged_when_no_known_prefix(self):
        self.assertEqual(default_subname("custom-name"), "custom-name")

    def test_default_subname_empty_returns_none(self):
        self.assertIsNone(default_subname("local-"))
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
        # Auto-derived from `oneoff-mini-abc` (host=mini) -> subname "mini-abc"
        # (default_subname strips only the `oneoff-` prefix).
        self.assertIn("[FR.mini][mini-abc]", captured["title"])

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


class _StepClock:
    """Clock whose ``now()`` is fixed and only advances on ``sleep()`` -- so a
    polling loop that respects its deadline terminates deterministically (the
    deadline is reached purely by elapsed sleeps, no wall time)."""
    def __init__(self, step: float = 1.0):
        self.t = 0.0
        self.step = step

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += self.step


class InjectActivationTest(unittest.TestCase):
    """The 409 fix: submit_active_with_retry must wait for active+connected and
    retry the POST on HTTP 409 ('Session is not active') until the turn lands.
    These exercise the REAL submit seam (return codes), not a faked-away one --
    the previous tests stubbed submit to always-200 and so missed the live 409.
    """

    def _run(self, *, states, submits, log_ids=None, timeout=10.0):
        """Drive submit_active_with_retry over scripted GET-state and POST
        results. *states* / *submits* are lists consumed one per call (last
        entry repeats). *log_ids* (optional) is the sequence the log re-harvest
        returns (last repeats), default: always the initial id."""
        calls = {"state": 0, "submit": 0, "tail": 0}
        logs: list = []
        clock = _StepClock(step=1.0)

        def fetch_state(cfg, token, sid, log):
            i = min(calls["state"], len(states) - 1)
            calls["state"] += 1
            return states[i]  # (code, status, conn)

        def submit(cfg, token, sid, message, log):
            i = min(calls["submit"], len(submits) - 1)
            calls["submit"] += 1
            return submits[i]  # (code, body)

        def read_tail(path):
            if log_ids is None:
                return b"session_INITIAL"
            i = min(calls["tail"], len(log_ids) - 1)
            calls["tail"] += 1
            return f"session_{log_ids[i]}".encode()

        rc, sid = submit_active_with_retry(
            initial_sid="cse_INITIAL", message="hello", logpath=Path("/x.log"),
            timeout_secs=timeout, log=logs.append,
            submit=submit, get_token=lambda cfg, log: "TOKEN",
            fetch_state=fetch_state, read_tail=read_tail,
            sleep=clock.sleep, clock=clock.now, poll_secs=1.0,
        )
        return rc, sid, calls, logs

    def test_active_first_try_submits_once(self):
        rc, sid, calls, _ = self._run(
            states=[(200, "active", "connected")],
            submits=[(200, {})])
        self.assertEqual(rc, 0)
        self.assertEqual(sid, "cse_INITIAL")
        self.assertEqual(calls["submit"], 1)

    def test_not_active_then_active_waits_then_submits(self):
        # First two state polls report a not-yet-active pre-created session, the
        # third reports active -> exactly one submit, which 200s.
        rc, sid, calls, logs = self._run(
            states=[(200, "pending", "disconnected"),
                    (200, "pending", "connecting"),
                    (200, "active", "connected")],
            submits=[(200, {})])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["submit"], 1)
        self.assertEqual(calls["state"], 3)

    def test_409_then_active_retries_and_succeeds(self):
        # Session reports active, but the POST races and 409s once; the retry
        # (still active) then 200s. This is the exact live failure mode.
        rc, sid, calls, logs = self._run(
            states=[(200, "active", "connected"),
                    (200, "active", "connected")],
            submits=[(409, {"error": "Session is not active"}), (200, {})])
        self.assertEqual(rc, 0)
        self.assertEqual(calls["submit"], 2)
        self.assertTrue(any("409" in m for m in logs))

    def test_superseded_id_is_reharvested_from_log(self):
        # The log id changes (pre-created 015x… -> active 01An…). The loop must
        # adopt the latest log id and submit into THAT session.
        rc, sid, calls, logs = self._run(
            states=[(200, "pending", "disconnected"),   # initial 015x not active
                    (200, "active", "connected")],       # 01An is active
            submits=[(200, {})],
            log_ids=["015x", "01An"])
        self.assertEqual(rc, 0)
        self.assertEqual(sid, "cse_01An")
        self.assertTrue(any("superseded" in m for m in logs))

    def test_permanent_not_active_times_out_cleanly(self):
        # Never becomes active -> bounded rc=1 at the deadline, NOT a hang.
        rc, sid, calls, logs = self._run(
            states=[(200, "pending", "disconnected")],
            submits=[(200, {})],   # never reached
            timeout=5.0)
        self.assertEqual(rc, 1)
        self.assertEqual(calls["submit"], 0)
        self.assertTrue(any("TIMEOUT" in m for m in logs))

    def test_permanent_409_times_out_cleanly(self):
        # State says active but the POST always 409s -> bounded rc=1, no hang.
        rc, sid, calls, logs = self._run(
            states=[(200, "active", "connected")],
            submits=[(409, {"error": "Session is not active"})],
            timeout=5.0)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(calls["submit"], 2)  # retried, then gave up
        self.assertTrue(any("TIMEOUT" in m for m in logs))

    def test_hard_error_fails_fast_without_retry(self):
        # A 500 (not a 409) is a real error -> fail immediately, don't burn the
        # whole window retrying it.
        rc, sid, calls, logs = self._run(
            states=[(200, "active", "connected")],
            submits=[(500, {"err": "boom"})],
            timeout=30.0)
        self.assertEqual(rc, 1)
        self.assertEqual(calls["submit"], 1)

    def test_archived_harvested_id_bails_fast_not_full_timeout(self):
        # The harvested id is terminal (archived) and no live id supersedes it.
        # The loop must return RC_DEAD_SESSION on the FIRST state poll -- NOT
        # burn the whole timeout polling for an archived session to go active
        # (the live slow-loop bug). One state call, zero submits, fast bail.
        from remote_control.new_session import RC_DEAD_SESSION
        rc, sid, calls, logs = self._run(
            states=[(200, "archived", "connected")],
            submits=[(200, {})],   # never reached
            timeout=45.0)          # a LONG window -- proves we don't consume it
        self.assertEqual(rc, RC_DEAD_SESSION)
        self.assertEqual(calls["submit"], 0)
        self.assertEqual(calls["state"], 1)          # bailed on the first poll
        self.assertTrue(any("terminal" in m for m in logs))

    def test_archived_id_superseded_by_live_id_is_rescued(self):
        # The first harvest is an archived id; a re-harvest then surfaces a
        # fresh live id (current run's pre-created session). The loop must adopt
        # the fresh id and submit into IT rather than bailing on the dead one.
        rc, sid, calls, logs = self._run(
            states=[(200, "archived", "connected"),   # initial id is dead
                    (200, "active", "connected")],     # fresh id is live
            submits=[(200, {})],
            log_ids=["01Dead", "01Fresh"])
        self.assertEqual(rc, 0)
        self.assertEqual(sid, "cse_01Fresh")
        self.assertEqual(calls["submit"], 1)

    def test_inject_into_server_retries_409_then_titles_active_session(self):
        # End-to-end through inject_into_server: harvested id is not-yet-active,
        # the POST 409s once then 200s; the title PUT must target the session
        # that actually accepted the turn, and rc is 0.
        titled: dict = {}

        def fake_set_title(cfg, token, sid, title):
            titled["sid"], titled["title"] = sid, title
            return (200, {})

        state_seq = [(200, "active", "connected"), (200, "active", "connected")]
        submit_seq = [(409, {"error": "Session is not active"}), (200, {})]
        si = {"s": 0, "p": 0}

        def fetch_state(cfg, token, sid, log):
            i = min(si["s"], len(state_seq) - 1); si["s"] += 1
            return state_seq[i]

        def submit(cfg, token, sid, message, log):
            i = min(si["p"], len(submit_seq) - 1); si["p"] += 1
            return submit_seq[i]

        with tempfile.TemporaryDirectory() as d:
            dev = Path(d)
            logdir = dev / "logs"; logdir.mkdir()
            (logdir / "mini-dev.log").write_bytes(b"session_01AnbfphActive\n")
            env = _env(d, extra={"REMOTE_CONTROL_DEV": str(dev),
                                 "REMOTE_CONTROL_LOGDIR": str(logdir)})
            from remote_control.config import SupervisorConfig
            with mock.patch.dict(os.environ, env, clear=False):
                cfg = SupervisorConfig.from_env()
                logs: list = []
                # Drive the activation loop with a fake clock so no real sleeps.
                clock = _StepClock(step=1.0)
                with mock.patch.object(new_session.time, "sleep", clock.sleep), \
                     mock.patch.object(new_session.time, "monotonic", clock.now):
                    rc = inject_into_server(
                        server_name="mini-dev", cwd=dev, cfg=cfg,
                        prompt_body="do the dispatch", subname="dispatcher",
                        wait_timeout=10.0, log=logs.append,
                        waiter=lambda lp, to, **_: "cse_01AnbfphActive",
                        submit=submit, get_token=lambda cfg, log: "TOKEN",
                        set_title=fake_set_title, fetch_state=fetch_state)
        self.assertEqual(rc, 0)
        self.assertEqual(si["p"], 2)  # 409 then 200
        # Title went to the session that accepted the turn.
        self.assertEqual(titled["sid"], "cse_01AnbfphActive")
        self.assertIn("[dispatcher]", titled["title"])


class InjectDryRunTest(unittest.TestCase):
    """--inject-into combined with --dry-run must print the planned action and
    submit/retitle nothing -- the live bug was the inject branch returning
    before the dry-run guard, so `--dry-run` clobbered the dev session for
    real (issue #93)."""

    def test_inject_dry_run_submits_nothing_and_titles_nothing(self):
        submitted: list = []
        titled: list = []

        def fake_submit(cfg, token, sid, message, log):
            submitted.append((sid, message))
            return (200, {})

        def fake_set_title(cfg, token, sid, title):
            titled.append((sid, title))
            return (200, {})

        with tempfile.TemporaryDirectory() as d:
            logdir = Path(d) / "logs"
            logdir.mkdir()
            # Pre-seed the dev server's log so the waiter has a real cse_ to
            # surface in the dry-run output (mirrors the live inject path).
            (logdir / "mini-dev.log").write_bytes(b"session_01Harvested\n")
            env = _env(d, extra={"REMOTE_CONTROL_LOGDIR": str(logdir)})
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("sys.stdout", buf):
                rc = new_session.main(
                    ["--dir", d, "--inject-into", "mini-dev",
                     "--prompt", "do the dispatch", "--dry-run"],
                    popen=_FakePopen(), git_probe=lambda p: True,
                    waiter=lambda lp, to, **_: "cse_01Harvested",
                    submit=fake_submit, set_title=fake_set_title,
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        # The whole point of the fix: no submit, no title PUT.
        self.assertEqual(submitted, [])
        self.assertEqual(titled, [])
        # The plan was reported.
        out = buf.getvalue()
        self.assertIn("DRY-RUN inject", out)
        self.assertIn("mini-dev", out)
        self.assertIn("cse_01Harvested", out)
        self.assertIn("prompt :", out)

    def test_inject_dry_run_without_harvestable_log_still_succeeds(self):
        # The server log isn't there yet (e.g. dev server not yet up): dry-run
        # still prints what it WOULD do and submits nothing. Regression guard
        # against an attempt to read a missing log path in the dry-run branch.
        submitted: list = []
        with tempfile.TemporaryDirectory() as d:
            logdir = Path(d) / "logs"
            logdir.mkdir()  # exists, but no <server>.log inside
            env = _env(d, extra={"REMOTE_CONTROL_LOGDIR": str(logdir)})
            buf = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch("sys.stdout", buf):
                rc = new_session.main(
                    ["--dir", d, "--inject-into", "mini-dev",
                     "--prompt", "hi", "--dry-run"],
                    popen=_FakePopen(), git_probe=lambda p: True,
                    waiter=lambda lp, to, **_: None,
                    submit=lambda *a, **kw: (submitted.append(a) or (200, {})),
                    set_title=lambda *a, **kw: (200, {}),
                    get_token=lambda cfg, log: "TOKEN")
        self.assertEqual(rc, 0)
        self.assertEqual(submitted, [])
        self.assertIn("DRY-RUN inject", buf.getvalue())


class FetchSessionStateTest(unittest.TestCase):
    """fetch_session_state must surface status + connection_status (the two
    fields that distinguish a submittable session from a pre-created one that
    409s) out of the GET /sessions/{id} body, including under response_shape."""

    def _call(self, api_return):
        from remote_control.usage_limit import monitor
        from remote_control.config import UsageLimitConfig
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, _env(d), clear=False):
                cfg = UsageLimitConfig.from_env()
            with mock.patch.object(monitor, "api_request",
                                   lambda cfg, m, p, t: api_return):
                return monitor.fetch_session_state(
                    cfg, "TOK", "cse_X", lambda m: None)

    def test_active_connected_top_level(self):
        code, status, conn = self._call(
            (200, {"status": "active", "connection_status": "connected"}))
        self.assertEqual((code, status, conn), (200, "active", "connected"))

    def test_reads_response_shape_wrapper(self):
        code, status, conn = self._call(
            (200, {"response_shape": {"status": "pending",
                                      "connection_status": "disconnected"}}))
        self.assertEqual((status, conn), ("pending", "disconnected"))

    def test_non_200_returns_code_and_blanks(self):
        code, status, conn = self._call((409, {"error": "Session is not active"}))
        self.assertEqual((code, status, conn), (409, "", ""))

    def test_transport_error_returns_none_code(self):
        code, status, conn = self._call((None, "URLError"))
        self.assertEqual((code, status, conn), (None, "", ""))


class DevRootTitleFallbackTest(unittest.TestCase):
    """The dispatcher injects with cwd == the dev ROOT, which is not a git repo.
    initial_subname_title must fall back to a [DEV.<host>] prefix instead of
    returning None (the live 'could not derive repo … skipping [dispatcher]
    title tag')."""

    def test_dev_root_cwd_gets_dev_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d)
            title = initial_subname_title(
                dev, host="mini", subname="dispatcher", dev_root=dev,
                nicknames_file="/no/such/file")
        self.assertIsNotNone(title)
        self.assertIn("[DEV.mini][dispatcher]", title)
        self.assertIn("auto-spawned", title)

    def test_repo_under_dev_root_unaffected(self):
        # A real repo under dev root still derives its own nick (not DEV).
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d)
            repo = dev / "fake-repo"; repo.mkdir()
            title = initial_subname_title(
                repo, host="mini", subname="dispatcher", dev_root=dev,
                nicknames_file="/no/such/file")
        self.assertIn("[FR.mini][dispatcher]", title)
        self.assertNotIn("[DEV.", title)

    def test_other_unresolvable_cwd_still_returns_none(self):
        # A cwd that is neither the dev root nor a repo under it stays None.
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d) / "dev"; dev.mkdir()
            elsewhere = Path(d) / "elsewhere"; elsewhere.mkdir()
            self.assertIsNone(initial_subname_title(
                elsewhere, host="mini", subname="x", dev_root=dev,
                nicknames_file="/no/such/file"))


if __name__ == "__main__":
    unittest.main()
