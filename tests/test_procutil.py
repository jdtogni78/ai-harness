import unittest
from pathlib import Path

from remote_control import new_session
from remote_control.config import SupervisorConfig
from remote_control.discovery import Server
from remote_control.procutil import (
    _augment_path, run_marker_line, spawn_argv, spawn_env)


def _cfg(host="note"):
    return SupervisorConfig.from_env({"REMOTE_CONTROL_HOST": host})


class SpawnArgvTest(unittest.TestCase):
    def test_command_shape(self):
        cfg = _cfg()
        srv = Server("mm-AppOne", Path("/Users/x/dev/AppOne"), "worktree")
        argv = spawn_argv(srv, cfg)
        self.assertEqual(argv[1], "remote-control")
        self.assertEqual(argv[2:6], ["--name", "mm-AppOne", "--spawn", "worktree"])
        # --name must be immediately followed by --spawn (procutil.server_pid
        # greps "name <name> --spawn"); guard against arg reordering.
        i = argv.index("--name")
        self.assertEqual(argv[i + 2], "--spawn")

    def test_permission_mode_is_bypass(self):
        # Unattended workers must not hang on in-Claude permission prompts;
        # the global perm-gate hook (#23) is the actual safety layer.
        cfg = _cfg()
        srv = Server("mm-AppOne", Path("/Users/x/dev/AppOne"), "worktree")
        argv = spawn_argv(srv, cfg)
        self.assertEqual(argv[argv.index("--permission-mode") + 1],
                         "bypassPermissions")


class RunMarkerTest(unittest.TestCase):
    """The run-start marker stamped into <name>.log before each (re)spawn so the
    dispatcher inject harvests only the CURRENT run's session id (not a stale or
    archived one preserved across a restart)."""

    def test_marker_has_prefix_unique_token_and_newline(self):
        line = run_marker_line()
        self.assertTrue(line.startswith(b"### ai-harness run-start "))
        self.assertTrue(line.endswith(b"\n"))
        # Distinct token per call (default uses time+pid).
        self.assertNotEqual(run_marker_line(), run_marker_line())

    def test_marker_token_is_honoured_when_given(self):
        self.assertEqual(run_marker_line("RUN42"),
                         b"### ai-harness run-start RUN42\n")

    def test_marker_prefix_agrees_with_new_session(self):
        # procutil writes the marker; new_session.extract_session_id anchors to
        # it. If the two prefixes drift apart, run-anchoring silently breaks.
        from remote_control import procutil
        self.assertEqual(procutil._RUN_MARKER_PREFIX,
                         new_session._RUN_MARKER_PREFIX)
        # And a marker procutil writes is recognised by the harvester's anchor.
        tail = run_marker_line("X") + b"session_01Fresh?from=cli\n"
        self.assertEqual(new_session.extract_session_id(tail), "cse_01Fresh")


class SpawnEnvTest(unittest.TestCase):
    def test_sets_session_name_prefix_to_host(self):
        env = spawn_env(_cfg("note"), {"PATH": "/usr/bin"})
        self.assertEqual(env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"], "note")
        # PATH now has the Homebrew/user bins prepended ahead of the inherited dir.
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))
        self.assertTrue(env["PATH"].endswith(":/usr/bin"))

    def test_overrides_inherited_prefix(self):
        env = spawn_env(_cfg("mini"),
                        {"CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": "stale"})
        self.assertEqual(env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"], "mini")

    def test_does_not_mutate_base_env(self):
        base = {"PATH": "/usr/bin"}
        spawn_env(_cfg(), base)
        self.assertNotIn("CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX", base)
        self.assertEqual(base["PATH"], "/usr/bin")  # PATH augmentation is non-mutating


class AugmentPathTest(unittest.TestCase):
    def test_prepends_homebrew_and_expands_home(self):
        out = _augment_path("/usr/bin:/bin", "/Users/me").split(":")
        self.assertEqual(
            out,
            ["/Users/me/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
             "/usr/local/bin", "/usr/bin", "/bin"],
        )

    def test_dedupes_existing_entries(self):
        # /opt/homebrew/bin already on PATH -> not duplicated, original order kept.
        out = _augment_path("/opt/homebrew/bin:/usr/bin", "/Users/me").split(":")
        self.assertEqual(out.count("/opt/homebrew/bin"), 1)
        self.assertEqual(out[-2:], ["/opt/homebrew/bin", "/usr/bin"])

    def test_skips_home_dir_when_home_unknown(self):
        out = _augment_path("/usr/bin", None).split(":")
        self.assertNotIn("/.local/bin", out)
        self.assertEqual(out[0], "/opt/homebrew/bin")

    def test_empty_path(self):
        self.assertEqual(
            _augment_path("", "/Users/me"),
            "/Users/me/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin",
        )


if __name__ == "__main__":
    unittest.main()
