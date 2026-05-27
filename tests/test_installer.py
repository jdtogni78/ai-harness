import tempfile
import unittest
from pathlib import Path

from remote_control.installer import (
    ACTIVE_FILE_TEMPLATE,
    AGENTS,
    agent_user,
    install_agent,
    launchctl_commands,
    plan_install,
    seed_active_file,
)


class PlanInstallTest(unittest.TestCase):
    def test_all_when_no_filter(self):
        self.assertEqual([p for p, _ in plan_install(AGENTS)], AGENTS)

    def test_labels_strip_plist_suffix(self):
        self.assertEqual(plan_install(AGENTS)[0][1], "com.user.claude-remote-control")

    def test_filter_by_label(self):
        self.assertEqual(
            plan_install(AGENTS, "com.user.claude-remote-control"),
            [("com.user.claude-remote-control.plist", "com.user.claude-remote-control")],
        )

    def test_filter_by_plist_name(self):
        pairs = plan_install(AGENTS, "com.user.claude-usage-limit-monitor.plist")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][1], "com.user.claude-usage-limit-monitor")

    def test_filter_no_match(self):
        self.assertEqual(plan_install(AGENTS, "nope"), [])

    def test_codex_relay_is_gone(self):
        self.assertFalse(any("codex" in a for a in AGENTS))

    def test_agent_user_token(self):
        self.assertEqual(agent_user("com.user2.claude-remote-control"), "user2")
        self.assertEqual(agent_user("com.user.claude-usage-limit-monitor.plist"), "user")
        self.assertEqual(agent_user("weird"), "")

    def test_default_filters_by_current_user(self):
        labels = {lbl for _, lbl in plan_install(AGENTS, current_user="user2")}
        self.assertEqual(labels, {"com.user2.claude-remote-control",
                                  "com.user2.claude-usage-limit-monitor"})
        labels = {lbl for _, lbl in plan_install(AGENTS, current_user="user")}
        self.assertEqual(labels, {
            "com.user.claude-remote-control",
            "com.user.claude-usage-limit-monitor",
        })

    def test_unknown_user_selects_nothing(self):
        self.assertEqual(plan_install(AGENTS, current_user="nobody"), [])

    def test_explicit_filter_overrides_user(self):
        # An explicit label installs even when it isn't the current user's.
        pairs = plan_install(AGENTS, "com.user.claude-remote-control", current_user="user2")
        self.assertEqual([lbl for _, lbl in pairs], ["com.user.claude-remote-control"])


class LaunchctlCommandsTest(unittest.TestCase):
    def test_order_ignore_flags_and_domain(self):
        cmds = launchctl_commands(501, "com.x", "/p/x.plist")
        self.assertEqual(cmds[0], (["launchctl", "bootout", "gui/501", "/p/x.plist"], True))
        self.assertEqual(cmds[1], (["launchctl", "bootstrap", "gui/501", "/p/x.plist"], False))
        self.assertEqual(cmds[2], (["launchctl", "enable", "gui/501/com.x"], False))


class _FakeResult:
    returncode = 0
    stdout = "state = running\n\tpid = 123\n"
    stderr = ""


class InstallAgentTest(unittest.TestCase):
    def test_copies_plist_and_runs_commands(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            la = Path(d) / "la"
            (repo / "com.x.plist").write_text("<plist/>")
            calls = []
            out = []

            def runner(argv, **kw):
                calls.append(argv)
                return _FakeResult()

            install_agent("com.x.plist", "com.x", 501, repo, la, runner=runner, out=out.append)

            self.assertTrue((la / "com.x.plist").exists())
            self.assertIn(["launchctl", "bootstrap", "gui/501", str(la / "com.x.plist")], calls)
            self.assertIn(["launchctl", "enable", "gui/501/com.x"], calls)
            self.assertTrue(any("Installed com.x" in o for o in out))


class SeedActiveFileTest(unittest.TestCase):
    def test_creates_file_with_template_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "active-dirs.txt"
            out = []
            created = seed_active_file(target, out=out.append)
            self.assertTrue(created)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(), ACTIVE_FILE_TEMPLATE)
            # File chmod 600 = octal 0o600 (rw-------) on platforms that support it.
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            # Parent dir chmod 700.
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            # Logged something so the user knows.
            self.assertTrue(any("Seeded" in m for m in out))

    def test_idempotent_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "active-dirs.txt"
            target.write_text("# already configured\nmy-app\n")
            out = []
            created = seed_active_file(target, out=out.append)
            self.assertFalse(created)
            # Content untouched.
            self.assertEqual(target.read_text(), "# already configured\nmy-app\n")
            # No "Seeded" log.
            self.assertFalse(any("Seeded" in m for m in out))


if __name__ == "__main__":
    unittest.main()
