import json
import tempfile
import unittest
from pathlib import Path

from remote_control import new_session, procutil
from remote_control.config import SupervisorConfig
from remote_control.discovery import Server
from remote_control.procutil import (
    _augment_path, _dir_slug, clear_bridge_pointer, run_marker_line,
    spawn, spawn_argv, spawn_env)


def _cfg(host="note", **env):
    base = {"REMOTE_CONTROL_HOST": host}
    base.update(env)
    return SupervisorConfig.from_env(base)


def _write_pointer(projects_root: Path, slug: str, **extra) -> Path:
    """Create a fake ``<projects_root>/<slug>/bridge-pointer.json`` mirroring the
    real shape (sessionId/environmentId/source/pid/procStart). Returns its path."""
    d = projects_root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / "bridge-pointer.json"
    payload = {
        "sessionId": "session_01Stale",
        "environmentId": "env_01Stale",
        "source": "standalone",
        "pid": 13077,
        "procStart": "Sun Jun 14 22:22:44 2026",
    }
    payload.update(extra)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


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


class ClearBridgePointerTest(unittest.TestCase):
    """clear_bridge_pointer removes the dev dir's stale pointer (so the next
    (re)spawn mints a fresh env/session) and never touches another dir's pointer
    or raises. Fully hermetic: an injected *home*/projects-root under a temp dir,
    so no real ``~/.claude/projects`` pointer is ever read or deleted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.projects = self.home / ".claude" / "projects"
        self.projects.mkdir(parents=True)
        # A "dev" dir to target plus a sibling whose pointer must survive.
        self.devdir = self.home / "dev"
        self.devdir.mkdir()
        self.otherdir = self.home / "dev" / "other"
        self.otherdir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_deletes_matching_pointer_by_slug(self):
        slug = _dir_slug(self.devdir)
        ptr = _write_pointer(self.projects, slug)
        out = clear_bridge_pointer(self.devdir, home=self.home)
        self.assertEqual(out, ptr)
        self.assertFalse(ptr.exists())

    def test_leaves_other_dirs_pointer_intact(self):
        dev_slug = _dir_slug(self.devdir)
        other_slug = _dir_slug(self.otherdir)
        dev_ptr = _write_pointer(self.projects, dev_slug)
        other_ptr = _write_pointer(self.projects, other_slug)
        clear_bridge_pointer(self.devdir, home=self.home)
        self.assertFalse(dev_ptr.exists())
        self.assertTrue(other_ptr.exists(), "sibling dir's pointer must survive")

    def test_prefers_dir_field_when_present(self):
        # If a future CLI records a dir field, match on it even under a wrong
        # slug dir name; the correct-slug pointer for a DIFFERENT dir is left.
        weird = self.projects / "some-unrelated-slug"
        weird.mkdir()
        ptr = weird / "bridge-pointer.json"
        ptr.write_text(json.dumps({"source": "standalone",
                                   "directory": str(self.devdir)}),
                       encoding="utf-8")
        out = clear_bridge_pointer(self.devdir, home=self.home)
        self.assertEqual(out, ptr)
        self.assertFalse(ptr.exists())

    def test_missing_pointer_is_noop(self):
        # No pointer anywhere -> returns None, no raise.
        self.assertIsNone(clear_bridge_pointer(self.devdir, home=self.home))

    def test_missing_projects_root_is_noop(self):
        empty_home = Path(self._tmp.name) / "nohome"
        self.assertIsNone(clear_bridge_pointer(self.devdir, home=empty_home))

    def test_unreadable_pointer_is_noop_and_still_slug_matches(self):
        # A garbage/non-JSON pointer at the right slug must NOT raise; the slug
        # fallback still selects and deletes it.
        slug = _dir_slug(self.devdir)
        d = self.projects / slug
        d.mkdir(parents=True)
        ptr = d / "bridge-pointer.json"
        ptr.write_text("not json {{{", encoding="utf-8")
        out = clear_bridge_pointer(self.devdir, home=self.home)
        self.assertEqual(out, ptr)
        self.assertFalse(ptr.exists())

    def test_logger_receiving_exception_is_swallowed(self):
        # A log callback that raises must not propagate out of the helper.
        def boom(_msg):
            raise RuntimeError("log sink down")
        # No pointer -> takes the "no pointer" log path -> must not raise.
        self.assertIsNone(
            clear_bridge_pointer(self.devdir, home=self.home, log=boom))


class SpawnClearsBridgePointerTest(unittest.TestCase):
    """spawn() clears the bridge pointer ONLY for the ``<host>-dev`` server when
    dispatcher_autospawn is on -- never for other servers, never when autospawn
    is off. Asserted via a spy on procutil.clear_bridge_pointer; subprocess.Popen
    is stubbed so no real ``claude`` is launched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.devdir = self.root / "dev"
        self.devdir.mkdir()
        self.logdir = self.root / "logs"
        self.logdir.mkdir()
        # Spy on the pointer-clear and stub Popen.
        self._calls = []
        self._orig_clear = procutil.clear_bridge_pointer
        self._orig_popen = procutil.subprocess.Popen
        procutil.clear_bridge_pointer = lambda directory, **kw: \
            self._calls.append(Path(directory))
        procutil.subprocess.Popen = lambda *a, **kw: object()

    def tearDown(self):
        procutil.clear_bridge_pointer = self._orig_clear
        procutil.subprocess.Popen = self._orig_popen
        self._tmp.cleanup()

    def _cfg_for(self, autospawn):
        return _cfg(
            host="note",
            REMOTE_CONTROL_DEV=str(self.devdir),
            REMOTE_CONTROL_LOGDIR=str(self.logdir),
            DISPATCHER_AUTOSPAWN="1" if autospawn else "0",
        )

    def test_clears_for_host_dev_when_autospawn_on(self):
        cfg = self._cfg_for(autospawn=True)
        srv = Server("note-dev", self.devdir, "same-dir")
        spawn(srv, cfg)
        self.assertEqual(self._calls, [self.devdir])

    def test_does_not_clear_for_other_server(self):
        cfg = self._cfg_for(autospawn=True)
        srv = Server("note-AppOne", self.devdir, "worktree")
        spawn(srv, cfg)
        self.assertEqual(self._calls, [])

    def test_does_not_clear_when_autospawn_off(self):
        cfg = self._cfg_for(autospawn=False)
        srv = Server("note-dev", self.devdir, "same-dir")
        spawn(srv, cfg)
        self.assertEqual(self._calls, [])

    def test_does_not_clear_when_directory_missing(self):
        # spawn() short-circuits (returns None) for a gone dir before any clear.
        cfg = self._cfg_for(autospawn=True)
        srv = Server("note-dev", self.root / "gone", "same-dir")
        self.assertIsNone(spawn(srv, cfg))
        self.assertEqual(self._calls, [])


if __name__ == "__main__":
    unittest.main()
