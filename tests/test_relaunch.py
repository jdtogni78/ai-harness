"""Unit tests for :mod:`remote_control.relaunch`.

Coverage:
  * Source resolution from --from CSE_ID (project dir scan) and from
    --from-transcript PATH (explicit, falls back to transcript's own cwd).
  * Version-bump + title swap helpers.
  * main() happy path with injected spawn + retitle (no network).
  * Idempotency gate (re-run on same source -> skip; --force bypasses).
  * Dry-run prints brief metadata without writing a handoff record.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from remote_control import relaunch
from remote_control.handoff import HANDOFFS_DIRNAME, write_handoff_record


def _user_line(text, cwd="/Users/me/dev/foo", sid="src"):
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text},
        "sessionId": sid, "cwd": cwd,
    })


def _make_source_layout(root: Path, cse_id: str, *,
                        cwd="/Users/me/dev/foo/.claude/worktrees/bridge-cse_xx",
                        turns=("first turn", "second turn")) -> Path:
    """Drop a `~/.claude/projects/<dir>/<uuid>.jsonl` layout under *root* whose
    dir name encodes *cse_id* via the same encoding used on disk."""
    # The project dir name must match what cse_id_from_project_dirname extracts:
    # any dir whose name ends with `bridge-cse-<tail>` resolves to `cse_<tail>`.
    tail = cse_id.removeprefix("cse_")
    dirname = f"-Users-me-dev-foo--claude-worktrees-bridge-cse-{tail}"
    proj = root / dirname
    proj.mkdir(parents=True)
    jl = proj / "abc.jsonl"
    jl.write_text("\n".join(_user_line(t, cwd=cwd) for t in turns) + "\n")
    return jl


class FindSourceTranscriptTest(unittest.TestCase):
    def test_finds_match_in_correctly_encoded_dirname(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jl = _make_source_layout(root, "cse_01ABC")
            got = relaunch.find_source_transcript("cse_01ABC", root)
            self.assertEqual(got, jl)

    def test_returns_none_when_no_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_source_layout(root, "cse_01OTHER")
            self.assertIsNone(
                relaunch.find_source_transcript("cse_01ABC", root))

    def test_picks_newest_when_multiple_jsonl_in_same_dir(self):
        # Simulates the case after a previous fork-all: the source dir contains
        # the source's transcript and one or more sibling forks; the source's
        # own (live) transcript has the newest mtime.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jl = _make_source_layout(root, "cse_01ABC")
            other = jl.parent / "older.jsonl"
            other.write_text("[]\n")
            # Force `other` older than `jl` by touching jl forward.
            import os
            os.utime(other, (1000, 1000))
            os.utime(jl, (2000, 2000))
            got = relaunch.find_source_transcript("cse_01ABC", root)
            self.assertEqual(got, jl)


class ResolveSourceTest(unittest.TestCase):
    def test_from_cse_reads_cwd_from_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_source_layout(
                root, "cse_01ABC",
                cwd="/Users/me/dev/foo/.claude/worktrees/bridge-cse_xx",
            )
            src = relaunch.resolve_source(
                cse_id="cse_01ABC", transcript_arg=None,
                cwd_arg=None, projects_root=root,
            )
            self.assertEqual(
                src.cwd,
                "/Users/me/dev/foo/.claude/worktrees/bridge-cse_xx")
            self.assertTrue(src.transcript.is_file())

    def test_from_transcript_with_explicit_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "t.jsonl"
            t.write_text(_user_line("hi") + "\n")
            src = relaunch.resolve_source(
                cse_id=None, transcript_arg=str(t),
                cwd_arg="/some/where", projects_root=root,
            )
            self.assertEqual(src.cwd, "/some/where")
            self.assertEqual(src.transcript, t)

    def test_from_transcript_falls_back_to_embedded_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "t.jsonl"
            t.write_text(_user_line("hi", cwd="/embed/here") + "\n")
            src = relaunch.resolve_source(
                cse_id=None, transcript_arg=str(t),
                cwd_arg=None, projects_root=root,
            )
            self.assertEqual(src.cwd, "/embed/here")

    def test_mutually_exclusive_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                relaunch.resolve_source(
                    cse_id="cse_01ABC", transcript_arg="/tmp/x",
                    cwd_arg=None, projects_root=Path(td))

    def test_neither_input_provided(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                relaunch.resolve_source(
                    cse_id=None, transcript_arg=None,
                    cwd_arg=None, projects_root=Path(td))

    def test_cse_with_no_transcript_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                relaunch.resolve_source(
                    cse_id="cse_01MISSING", transcript_arg=None,
                    cwd_arg=None, projects_root=Path(td))


class BumpVersionTest(unittest.TestCase):
    def test_no_version_appends_v2(self):
        # First relaunch of an un-versioned title: the original is implicit v1,
        # so the first relaunch becomes v2. Matches the user's mental model.
        self.assertEqual(relaunch.bump_version("Resume prep"), "Resume prep v2")

    def test_existing_v2_bumps_to_v3(self):
        self.assertEqual(relaunch.bump_version("Resume prep v2"), "Resume prep v3")

    def test_v10_bumps_to_v11(self):
        # Two-digit handling matters; we don't want v1 -> v20 from a regex tail
        # match against the last digit.
        self.assertEqual(relaunch.bump_version("foo v10"), "foo v11")

    def test_empty_body_becomes_v2(self):
        self.assertEqual(relaunch.bump_version(""), "v2")
        self.assertEqual(relaunch.bump_version("   "), "v2")

    def test_v_inside_body_not_matched(self):
        # "v2 prep" inside the body must NOT be treated as the version tag --
        # only a trailing ` vN` qualifies.
        self.assertEqual(
            relaunch.bump_version("Resume v2 prep"), "Resume v2 prep v2")


class SwapTitleBodyTest(unittest.TestCase):
    def test_swaps_under_double_bracket_prefix(self):
        got = relaunch.swap_title_body(
            "[JOB.mini][relaunch-01ABC] auto-spawned", "Resume prep v2")
        self.assertEqual(got, "[JOB.mini][relaunch-01ABC] Resume prep v2")

    def test_swaps_under_single_bracket_prefix(self):
        got = relaunch.swap_title_body("[JOB.mini] old body", "new body")
        self.assertEqual(got, "[JOB.mini] new body")

    def test_no_prefix_returns_body_verbatim(self):
        self.assertEqual(
            relaunch.swap_title_body("plain text", "new body"), "new body")


class ComputeRelaunchTitleTest(unittest.TestCase):
    def test_inherit_and_bump_under_spawn_prefix(self):
        # Spawn just set "[JOB.mini][relaunch-XX] auto-spawned"; source title
        # was "[JOB.mini] Resume preparation". The new title preserves the
        # spawn's [NICK.host][relaunch-XX] prefix, and the body is the bumped
        # source body.
        got = relaunch.compute_relaunch_title(
            source_title="[JOB.mini] Resume preparation",
            spawned_title="[JOB.mini][relaunch-01ABC] auto-spawned",
        )
        self.assertEqual(got, "[JOB.mini][relaunch-01ABC] Resume preparation v2")

    def test_source_already_versioned_advances(self):
        got = relaunch.compute_relaunch_title(
            source_title="[JOB.mini] Resume preparation v2",
            spawned_title="[JOB.mini][relaunch-01ABC] auto-spawned",
        )
        self.assertEqual(got, "[JOB.mini][relaunch-01ABC] Resume preparation v3")

    def test_empty_source_returns_none(self):
        self.assertIsNone(relaunch.compute_relaunch_title(
            source_title=None, spawned_title="[NICK][SUB] auto-spawned"))
        self.assertIsNone(relaunch.compute_relaunch_title(
            source_title="[NICK.host] ", spawned_title="x"))


class MainHappyPathTest(unittest.TestCase):
    """End-to-end main() with all I/O injected. Verifies that on first run we:
    spawn -> retitle -> write the handoff record; on second run we skip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir()
        self.state = self.root / "state"
        _make_source_layout(self.projects, "cse_01ABC")
        self.spawn_calls = []
        self.retitle_calls = []

        def fake_spawn(*, cwd, brief, subname, wait_timeout, log):
            self.spawn_calls.append({
                "cwd": cwd, "subname": subname, "brief_bytes": len(brief),
                "wait_timeout": wait_timeout,
            })
            return "cse_01NEW"

        def fake_retitle(*, source_cse, new_cse, log):
            self.retitle_calls.append((source_cse, new_cse))
            return True, "[JOB.mini][relaunch-XX] Foo v2"

        self.fake_spawn = fake_spawn
        self.fake_retitle = fake_retitle

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = relaunch.main(
                list(args),
                spawn=self.fake_spawn,
                retitle=self.fake_retitle,
                projects_root=self.projects,
                env={"HOME": str(self.root)},
                now_iso="2026-06-01T00:00:00+00:00",
            )
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_first_run_spawns_writes_record_returns_zero(self):
        rc, out, err = self._run("--from", "cse_01ABC",
                                 "--state-dir", str(self.state))
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(len(self.spawn_calls), 1)
        self.assertEqual(self.spawn_calls[0]["subname"], "relaunch-01ABC")
        self.assertEqual(self.retitle_calls, [("cse_01ABC", "cse_01NEW")])
        # Handoff record landed under state/handoffs/<new_cse>.json:
        rec = self.state / HANDOFFS_DIRNAME / "cse_01NEW.json"
        self.assertTrue(rec.is_file(), f"missing {rec}")
        body = json.loads(rec.read_text())
        self.assertEqual(body["new_cse"], "cse_01NEW")
        self.assertEqual(body["forked_uuid"], "cse_01ABC")
        self.assertEqual(body["source_cse"], "cse_01ABC")

    def test_second_run_idempotency_gate_skips(self):
        # First run lands a record; second run on the same source should skip.
        self._run("--from", "cse_01ABC", "--state-dir", str(self.state))
        self.spawn_calls.clear()
        self.retitle_calls.clear()
        rc, out, err = self._run("--from", "cse_01ABC",
                                 "--state-dir", str(self.state))
        self.assertEqual(rc, 0)
        self.assertEqual(self.spawn_calls, [],
                         "spawn must NOT fire on second run")
        self.assertIn("skip", err.lower())

    def test_force_bypasses_idempotency(self):
        self._run("--from", "cse_01ABC", "--state-dir", str(self.state))
        self.spawn_calls.clear()
        rc, out, err = self._run("--from", "cse_01ABC", "--force",
                                 "--state-dir", str(self.state))
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(len(self.spawn_calls), 1,
                         "--force must spawn even with existing record")

    def test_no_retitle_skips_retitle_call(self):
        rc, out, err = self._run("--from", "cse_01ABC", "--no-retitle",
                                 "--state-dir", str(self.state))
        self.assertEqual(rc, 0, msg=err)
        self.assertEqual(self.spawn_calls[0]["subname"], "relaunch-01ABC")
        self.assertEqual(self.retitle_calls, [],
                         "--no-retitle must NOT call retitle")

    def test_dry_run_prints_brief_without_spawning(self):
        rc, out, err = self._run("--from", "cse_01ABC", "--dry-run",
                                 "--state-dir", str(self.state))
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out)
        self.assertEqual(self.spawn_calls, [],
                         "dry-run must not spawn")
        # No handoff record:
        self.assertFalse(
            (self.state / HANDOFFS_DIRNAME).exists()
            and any((self.state / HANDOFFS_DIRNAME).iterdir()),
            "dry-run must not write a handoff record",
        )

    def test_spawn_failure_returns_nonzero_and_no_record(self):
        def failing_spawn(**kwargs):
            return None
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = relaunch.main(
                ["--from", "cse_01ABC", "--state-dir", str(self.state)],
                spawn=failing_spawn,
                retitle=self.fake_retitle,
                projects_root=self.projects,
                env={"HOME": str(self.root)},
                now_iso="2026-06-01T00:00:00+00:00",
            )
        self.assertEqual(rc, 1)
        self.assertEqual(self.retitle_calls, [],
                         "no retitle when spawn failed")
        self.assertFalse(
            (self.state / HANDOFFS_DIRNAME).exists()
            and any((self.state / HANDOFFS_DIRNAME).iterdir()),
            "no record when spawn failed",
        )


if __name__ == "__main__":
    unittest.main()
