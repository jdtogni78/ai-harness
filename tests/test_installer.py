import os
import tempfile
import unittest
from pathlib import Path

from remote_control.installer import (
    ACTIVE_FILE_SAMPLE,
    AGENTS,
    _ACTIVE_FILE_FALLBACK,
    _host_file_seed_body,
    agent_user,
    install_agent,
    launchctl_commands,
    plan_install,
    seed_active_file,
    seed_host_file,
)
from remote_control.config import host_nickname


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
                                  "com.user2.claude-usage-limit-monitor",
                                  "com.user2.claude-titles-monitor"})
        labels = {lbl for _, lbl in plan_install(AGENTS, current_user="user")}
        self.assertEqual(labels, {
            "com.user.claude-remote-control",
            "com.user.claude-usage-limit-monitor",
            "com.user.claude-titles-monitor",
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
    def test_creates_file_with_sample_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "active-dirs.txt"
            out = []
            created = seed_active_file(target, out=out.append)
            self.assertTrue(created)
            self.assertTrue(target.exists())
            # The seeded content matches the committed sample (source of truth).
            self.assertEqual(target.read_text(), ACTIVE_FILE_SAMPLE.read_text())
            # File chmod 600 = octal 0o600 (rw-------) on platforms that support it.
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            # Parent dir chmod 700.
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            # Log names the sample file so an operator can find what was copied.
            self.assertTrue(any(ACTIVE_FILE_SAMPLE.name in m for m in out))

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

    def test_uses_explicit_sample_override(self):
        # The sample arg is overridable so callers (and these tests) can point
        # the seed at any committed sample without depending on the repo layout.
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "my.example.txt"
            sample.write_text("# custom\nexplicit-entry\n")
            target = Path(tmp) / "active-dirs.txt"
            seed_active_file(target, out=lambda _m: None, sample=sample)
            self.assertEqual(target.read_text(), "# custom\nexplicit-entry\n")

    def test_falls_back_when_sample_missing(self):
        # If the committed sample is somehow unreachable (zipped install,
        # partial checkout) the seed still produces a comment-only allowlist,
        # not an empty file (an empty allowlist would deactivate every server).
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.txt"
            target = Path(tmp) / "active-dirs.txt"
            seed_active_file(target, out=lambda _m: None, sample=missing)
            self.assertEqual(target.read_text(), _ACTIVE_FILE_FALLBACK)

    def test_committed_sample_has_no_active_entries(self):
        # Safety check: the repo sample MUST stay entry-free. If someone adds a
        # real basename here, every fresh checkout would auto-spawn a server for
        # that name on its next supervisor tick (the swap-incident shape).
        for line in ACTIVE_FILE_SAMPLE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                self.fail(f"active-dirs.example.txt contains an active entry: "
                          f"{line!r}")


class SeedHostFileTest(unittest.TestCase):
    """Mirror :class:`SeedActiveFileTest` for the new host-nick seed (#32)."""

    def test_creates_file_with_value_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "host"
            out = []
            created = seed_host_file(target, "mini", out=out.append)
            self.assertTrue(created)
            self.assertTrue(target.exists())
            # The seeded body contains the value on its own line plus the
            # explanatory header. host_nickname must round-trip the value.
            self.assertIn("\nmini\n", target.read_text())
            self.assertEqual(host_nickname({}, host_file=str(target)), "mini")
            # File 600, dir 700 -- same regime as seed_active_file.
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertTrue(any("mini" in m for m in out))

    def test_idempotent_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "host"
            target.write_text("# operator-renamed\nnote\n")
            out = []
            created = seed_host_file(target, "mini", out=out.append)
            self.assertFalse(created)
            # The operator's earlier file wins (the FILE is authoritative
            # post-install per the spec). seed_host_file must not clobber it
            # even if a different host_value is passed in.
            self.assertEqual(target.read_text(), "# operator-renamed\nnote\n")
            self.assertFalse(any("Seeded" in m for m in out))

    def test_empty_host_value_is_no_op(self):
        # No REMOTE_CONTROL_HOST in the plist -> nothing to capture. Don't
        # create a comment-only file; that would mask the hostname-derive
        # path at run time and silently break the supervisor's nickname.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "host"
            for value in ("", "   ", "\n"):
                with self.subTest(value=repr(value)):
                    if target.exists():
                        target.unlink()
                    out = []
                    created = seed_host_file(target, value, out=out.append)
                    self.assertFalse(created)
                    self.assertFalse(target.exists())
                    self.assertFalse(any("Seeded" in m for m in out))

    def test_strips_whitespace_around_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "host"
            seed_host_file(target, "  note  ", out=lambda _m: None)
            # Whitespace stripped before write; round-trips through host_nickname.
            self.assertIn("\nnote\n", target.read_text())
            self.assertEqual(host_nickname({}, host_file=str(target)), "note")

    def test_seeded_body_has_explanatory_header(self):
        # Operators reading the file should see WHY editing it is the right
        # post-install knob (instead of editing the plist). Lock that in.
        body = _host_file_seed_body("mini")
        self.assertIn("#", body)
        # The file is consumed by host_nickname's comment-tolerant reader,
        # so leading comment lines must not break the parse.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "host"
            target.write_text(body)
            self.assertEqual(host_nickname({}, host_file=str(target)), "mini")


if __name__ == "__main__":
    unittest.main()
