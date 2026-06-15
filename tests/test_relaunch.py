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

    def test_fetch_events_fallback_used_when_no_local_transcript(self):
        # Cloud-agent / cross-host sessions don't write a local JSONL; the
        # caller injects ``fetch_events`` and we should fall through to it
        # instead of raising FileNotFoundError.
        with tempfile.TemporaryDirectory() as td:
            events = [{"event_type": "user", "source": "client",
                       "payload": {"message": {"role": "user",
                                               "content": "hi from cloud"}},
                       "sequence_num": 1}]

            def fake_fetch(cse_id):
                self.assertEqual(cse_id, "cse_01CLOUD")
                return ("/Users/x/dev/cloudrepo", events)

            src = relaunch.resolve_source(
                cse_id="cse_01CLOUD", transcript_arg=None,
                cwd_arg=None, projects_root=Path(td),
                fetch_events=fake_fetch,
            )
            self.assertIsNone(src.transcript)
            self.assertEqual(src.cwd, "/Users/x/dev/cloudrepo")
            self.assertEqual(src.events, events)
            self.assertIn("cse_01CLOUD", src.source_label or "")

    def test_fetch_events_fallback_respects_cwd_override(self):
        # When the API didn't yield a cwd (session-init event paged off) the
        # caller can still supply ``--cwd`` and the fetcher's None is OK.
        with tempfile.TemporaryDirectory() as td:
            events = [{"event_type": "user", "source": "client",
                       "payload": {"message": {"role": "user",
                                               "content": "hi"}},
                       "sequence_num": 1}]
            src = relaunch.resolve_source(
                cse_id="cse_01CLOUD", transcript_arg=None,
                cwd_arg="/override/cwd", projects_root=Path(td),
                fetch_events=lambda _id: (None, events),
            )
            self.assertEqual(src.cwd, "/override/cwd")
            self.assertEqual(src.events, events)

    def test_fetch_events_returning_none_still_raises(self):
        # Fetcher itself failed (no token, API 5xx) -> we should report
        # FileNotFoundError so the failure mode matches the no-fetcher case.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                relaunch.resolve_source(
                    cse_id="cse_01CLOUD", transcript_arg=None,
                    cwd_arg=None, projects_root=Path(td),
                    fetch_events=lambda _id: None,
                )

    def test_fetch_events_yields_no_cwd_and_no_override_raises(self):
        # API returned events but no cwd, and the user didn't pass --cwd -->
        # ValueError naming the missing argument, NOT a silent spawn into ``.``.
        with tempfile.TemporaryDirectory() as td:
            events = [{"event_type": "user", "source": "client",
                       "payload": {"message": {"role": "user",
                                               "content": "hi"}},
                       "sequence_num": 1}]
            with self.assertRaises(ValueError):
                relaunch.resolve_source(
                    cse_id="cse_01CLOUD", transcript_arg=None,
                    cwd_arg=None, projects_root=Path(td),
                    fetch_events=lambda _id: (None, events),
                )


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


class ExtractSpawnedCseTest(unittest.TestCase):
    """Regression: relaunch must recognise the actual new-session sentinel.

    new-session prints ``  session: cse_...`` (leading indent, no space
    before the colon) on both the inject and spawn paths. relaunch used to
    grep for ``session :`` (space before the colon) and reported a false
    "spawn failed" even though the new bridge was alive (#94).
    """

    # Representative tail of a real new-session spawn (see new_session.py line
    # 895): startup notices, then the indented `  session: cse_...` line.
    _SPAWN_STDOUT = (
        "new-session: launched (pid 12345)\n"
        "new-session: waiting for inner session to register (timeout 60s)\n"
        "  session: cse_01HZSPAWNED\n"
    )

    # Representative inject-path output (new_session.py line 685 / 687):
    # `inject: ...` line followed by the indented `  session: cse_...`.
    _INJECT_STDOUT = (
        "inject: target server oneoff-mini-deadbeef session cse_01HZINJECTED\n"
        "  session: cse_01HZINJECTED\n"
    )

    def test_parses_spawn_path_stdout(self):
        self.assertEqual(
            relaunch._extract_spawned_cse(self._SPAWN_STDOUT),
            "cse_01HZSPAWNED",
        )

    def test_parses_inject_path_stdout(self):
        self.assertEqual(
            relaunch._extract_spawned_cse(self._INJECT_STDOUT),
            "cse_01HZINJECTED",
        )

    def test_parses_legacy_session_space_colon_form(self):
        # Earlier docstring + comments described `session : cse_...` with a
        # space before the colon. Accept that too, so a future emitter swap
        # in either direction doesn't break the consumer again.
        self.assertEqual(
            relaunch._extract_spawned_cse("session : cse_01LEGACY\n"),
            "cse_01LEGACY",
        )

    def test_returns_none_when_sentinel_missing(self):
        self.assertIsNone(
            relaunch._extract_spawned_cse(
                "new-session: TIMEOUT waiting 60s for the inner session\n"))

    def test_returns_none_on_empty_stdout(self):
        self.assertIsNone(relaunch._extract_spawned_cse(""))

    def test_ignores_unrelated_session_word_in_other_lines(self):
        # A noise line that merely contains "session" somewhere must NOT be
        # treated as the sentinel; only the leading `session:` form counts.
        noisy = (
            "new-session: launched (pid 42)\n"
            "  inner session not ready yet\n"
            "  session: cse_01HZSPAWNED\n"
        )
        self.assertEqual(
            relaunch._extract_spawned_cse(noisy), "cse_01HZSPAWNED")


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
