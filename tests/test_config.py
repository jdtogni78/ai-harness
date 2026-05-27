import socket
import unittest
from pathlib import Path

from remote_control.config import SupervisorConfig, UsageLimitConfig
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
        self.assertEqual(c.manager_log, c.logdir / "manager.log")

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
