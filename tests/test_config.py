import socket
import tempfile
import unittest
from pathlib import Path

from remote_control.config import SupervisorConfig, UsageLimitConfig, host_nickname
from remote_control.discovery import nickname_from_hostname


class SupervisorConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = SupervisorConfig.from_env({})
        # Both DEV and CLAUDE_BIN are derived from $HOME so the same code works
        # on every account. A hardcoded "/Users/user/..." default would crash-loop
        # the supervisor on any other account (see incident 2026-05-26).
        self.assertEqual(c.dev, Path.home() / "dev")
        self.assertEqual(c.claude_bin, Path.home() / ".local/bin/claude")
        self.assertEqual(c.tick_secs, 30)
        self.assertEqual(c.idle_recycle_secs, 43200)
        self.assertEqual(c.grace_secs, 10)
        self.assertEqual(c.deactivate_min_strikes, 2)
        self.assertEqual(c.manager_log, c.logdir / "manager.log")

    def test_deactivate_min_strikes_floor_is_one(self):
        # The hysteresis is the whole point of this knob -- accept 1 (= old,
        # zero-tolerance behaviour, for debugging) but clamp anything lower up
        # to 1 so a misconfigured 0 / negative doesn't reintroduce the issue #8
        # oscillation by accident.
        for raw, want in (("1", 1), ("0", 1), ("-3", 1), ("3", 3)):
            with self.subTest(raw=raw):
                c = SupervisorConfig.from_env({"DEACTIVATE_MIN_STRIKES": raw})
                self.assertEqual(c.deactivate_min_strikes, want)

    def test_claude_bin_default_is_per_user(self):
        # Regression: the default used to be the literal "/Users/user/..." and
        # would crash any supervisor whose plist forgot REMOTE_CONTROL_CLAUDE_BIN.
        c = SupervisorConfig.from_env({})
        self.assertNotIn("/Users/user/", str(c.claude_bin))
        self.assertTrue(str(c.claude_bin).startswith(str(Path.home())))

    def test_claude_bin_env_override(self):
        c = SupervisorConfig.from_env({"REMOTE_CONTROL_CLAUDE_BIN": "/opt/claude"})
        self.assertEqual(c.claude_bin, Path("/opt/claude"))

    def test_env_overrides(self):
        c = SupervisorConfig.from_env({
            "REMOTE_CONTROL_DEV": "/tmp/dev",
            "REMOTE_CONTROL_LOGDIR": "/tmp/logs",
            "REMOTE_CONTROL_ACTIVE_FILE": "/tmp/active.txt",
            "TICK_SECS": "5",
            "IDLE_RECYCLE_SECS": "60",
            "GRACE_SECS": "2",
        })
        self.assertEqual(c.dev, Path("/tmp/dev"))
        self.assertEqual(c.logdir, Path("/tmp/logs"))
        self.assertEqual(c.active_file, Path("/tmp/active.txt"))
        self.assertEqual((c.tick_secs, c.idle_recycle_secs, c.grace_secs), (5, 60, 2))

    def test_host_explicit_normalized(self):
        self.assertEqual(SupervisorConfig.from_env({"REMOTE_CONTROL_HOST": " User "}).host,
                         "user")

    def test_host_defaults_to_hostname_nickname(self):
        self.assertEqual(SupervisorConfig.from_env({}).host,
                         nickname_from_hostname(socket.gethostname()))

    def test_handoff_perm_mode_default_is_bypass(self):
        # Handoff sessions come up unattended on supervisor restart -- any
        # mode that prompts (default / acceptEdits) leaves the worker hung
        # indefinitely waiting for input that no human will type. Safety is
        # enforced by the PreToolUse perm-gate hook, not by in-Claude
        # prompting. Locked in by this test so a careless edit doesn't
        # silently re-introduce a blocking default that strands every
        # respawned heir on its first tool call.
        self.assertEqual(SupervisorConfig.from_env({}).handoff_perm_mode,
                         "bypassPermissions")

    def test_handoff_perm_mode_env_override(self):
        # Passed through verbatim to ``claude --permission-mode`` so any value
        # the flag accepts (acceptEdits, plan, default, etc.) works.
        c = SupervisorConfig.from_env({"RESUME_ON_RESTART_PERM_MODE": "plan"})
        self.assertEqual(c.handoff_perm_mode, "plan")


class HostNicknamePrecedenceTest(unittest.TestCase):
    """The strict env > file > derive precedence from #32. Each test pins one
    tier of input and verifies the resolved value matches that tier."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.host_file = Path(self._tmp.name) / "host"

    def tearDown(self):
        self._tmp.cleanup()

    def test_env_wins_over_file_and_derive(self):
        self.host_file.write_text("from-file\n")
        nick = host_nickname({"REMOTE_CONTROL_HOST": "from-env"},
                             host_file=str(self.host_file))
        self.assertEqual(nick, "from-env")

    def test_env_is_normalized(self):
        # `normalize_host` lowercases + strips whitespace (same as the
        # existing env-only behavior).
        nick = host_nickname({"REMOTE_CONTROL_HOST": "  MyNick  "},
                             host_file=str(self.host_file))
        self.assertEqual(nick, "mynick")

    def test_file_wins_over_derive_when_env_unset(self):
        self.host_file.write_text("note\n")
        nick = host_nickname({}, host_file=str(self.host_file))
        self.assertEqual(nick, "note")

    def test_file_is_normalized(self):
        self.host_file.write_text("  Note  \n")
        nick = host_nickname({}, host_file=str(self.host_file))
        self.assertEqual(nick, "note")

    def test_file_skips_comments_and_blanks(self):
        self.host_file.write_text(
            "# header line\n"
            "\n"
            "  # indented comment\n"
            "note\n"
            "# trailing comment\n"
        )
        self.assertEqual(host_nickname({}, host_file=str(self.host_file)), "note")

    def test_inline_comment_stripped(self):
        self.host_file.write_text("note  # the macbook\n")
        self.assertEqual(host_nickname({}, host_file=str(self.host_file)), "note")

    def test_empty_env_falls_through_to_file(self):
        # env present but empty/whitespace -> treat as unset (the existing
        # ``.strip()`` semantics must be preserved through the new file tier).
        self.host_file.write_text("from-file\n")
        nick = host_nickname({"REMOTE_CONTROL_HOST": "   "},
                             host_file=str(self.host_file))
        self.assertEqual(nick, "from-file")

    def test_derive_when_both_env_and_file_absent(self):
        # No env, no file (file doesn't exist) -> hostname-derive.
        nick = host_nickname({}, host_file=str(self.host_file))
        self.assertEqual(nick, nickname_from_hostname(socket.gethostname()))

    def test_empty_file_falls_through_to_derive(self):
        # A file present but containing only comments/whitespace must NOT
        # mask the hostname-derive fallback -- otherwise an operator's
        # comment-only seed would silently break the supervisor's nickname.
        self.host_file.write_text("# only a header, no value\n\n")
        nick = host_nickname({}, host_file=str(self.host_file))
        self.assertEqual(nick, nickname_from_hostname(socket.gethostname()))


class UsageLimitConfigTest(unittest.TestCase):
    def test_dry_run_default_on(self):
        self.assertTrue(UsageLimitConfig.from_env({"HOME": "/Users/x"}).dry_run)

    def test_dry_run_off_values(self):
        for v in ("0", "false", "no", "", "  0  "):
            with self.subTest(v=v):
                c = UsageLimitConfig.from_env({"HOME": "/Users/x", "USAGE_LIMIT_DRY_RUN": v})
                self.assertFalse(c.dry_run)

    def test_dry_run_on_values(self):
        for v in ("1", "true", "yes"):
            with self.subTest(v=v):
                c = UsageLimitConfig.from_env({"HOME": "/Users/x", "USAGE_LIMIT_DRY_RUN": v})
                self.assertTrue(c.dry_run)

    def test_paths_and_skip_and_constants(self):
        c = UsageLimitConfig.from_env({
            "HOME": "/Users/x",
            "REMOTE_CONTROL_LOGDIR": "/tmp/l",
            "USAGE_LIMIT_SKIP_SIDS": "cse_a, cse_b ,",
        })
        self.assertEqual(c.state_file, Path("/tmp/l/paused-sessions.json"))
        self.assertEqual(c.log_file, Path("/tmp/l/usage-limit-monitor.log"))
        self.assertEqual(c.lock_file, Path("/tmp/l/usage-limit-monitor.lock"))
        self.assertEqual(c.skip_session_ids, frozenset({"cse_a", "cse_b"}))
        self.assertEqual(c.backoffs_secs, (300, 900, 1800))
        self.assertEqual(c.gc_age_secs, 7 * 24 * 3600)
        self.assertEqual(c.max_attempts, 0)


if __name__ == "__main__":
    unittest.main()
