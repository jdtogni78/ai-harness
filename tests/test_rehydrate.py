"""Unit tests for :mod:`remote_control.rehydrate`.

The pure helpers (orphan-classification, dead-oneoff split, marker I/O) drive
against a temp ``~/.claude/projects`` fixture and an in-memory live-session
list -- no real network, no real subprocess. The top-level entrypoints
(``rehydrate_supervisor_orphans``, ``sweep_oneoff_checkpoints``) get end-to-end
exercise via the same fixture, so the AC checklist on issue #29 maps 1:1 to
test names below.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from remote_control import rehydrate
from remote_control.rehydrate import (
    OneoffCheckpoint,
    OrphanCandidate,
    archived_marker_path,
    find_dead_oneoffs,
    find_oneoff_transcript,
    find_orphan_candidates,
    is_already_rehydrated,
    is_orphan_session,
    is_project_archived,
    load_oneoff_checkpoints,
    oneoff_checkpoint_path,
    read_rehydrated_marker,
    rehydrate_supervisor_orphans,
    remove_oneoff_checkpoint,
    sweep_oneoff_checkpoints,
    write_oneoff_checkpoint,
    write_rehydrated_marker,
)
from remote_control.session_fork import encode_project_dir


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class IsOrphanSessionTest(unittest.TestCase):
    def test_missing_session_is_orphan(self):
        # The cloud has no record -> there's no live process serving it.
        self.assertTrue(is_orphan_session(None))

    def test_disconnected_is_orphan(self):
        # After supervisor SIGTERMed its servers, every session reads as
        # disconnected. Those are exactly the rehydration targets.
        self.assertTrue(is_orphan_session({
            "id": "cse_x", "connection_status": "disconnected",
        }))

    def test_archived_is_not_orphan(self):
        # The user explicitly ended it -- don't yank it back into a picker.
        self.assertFalse(is_orphan_session({
            "id": "cse_x", "status": "archived",
            "connection_status": "disconnected",
        }))

    def test_connected_is_not_orphan(self):
        self.assertFalse(is_orphan_session({
            "id": "cse_x", "connection_status": "connected",
        }))


class FindDeadOneoffsTest(unittest.TestCase):
    def _cp(self, name="oneoff-x", pid=42, started_at="2026-05-31T00:00:00+00:00"):
        return OneoffCheckpoint(name=name, dir="/d", pid=pid,
                                started_at=started_at, session_id=None, path=None)

    def test_alive_pid_neither_recoverable_nor_stale(self):
        cp = self._cp()
        rec, stale = find_dead_oneoffs(
            [cp], alive=lambda _p: True, now=time.time(), ttl_secs=3600,
            started_at_to_epoch=lambda _ts: time.time(),
        )
        self.assertEqual(rec, [])
        self.assertEqual(stale, [])

    def test_dead_within_ttl_is_recoverable(self):
        cp = self._cp()
        rec, stale = find_dead_oneoffs(
            [cp], alive=lambda _p: False, now=1000.0, ttl_secs=3600,
            started_at_to_epoch=lambda _ts: 500.0,
        )
        self.assertEqual([c.name for c in rec], ["oneoff-x"])
        self.assertEqual(stale, [])

    def test_dead_past_ttl_is_stale(self):
        cp = self._cp()
        rec, stale = find_dead_oneoffs(
            [cp], alive=lambda _p: False, now=10000.0, ttl_secs=3600,
            started_at_to_epoch=lambda _ts: 500.0,
        )
        self.assertEqual(rec, [])
        self.assertEqual([c.name for c in stale], ["oneoff-x"])

    def test_unparseable_timestamp_is_stale(self):
        cp = self._cp(started_at="not a timestamp")
        rec, stale = find_dead_oneoffs(
            [cp], alive=lambda _p: False, now=1000.0, ttl_secs=3600,
            started_at_to_epoch=lambda _ts: None,
        )
        self.assertEqual(rec, [])
        self.assertEqual([c.name for c in stale], ["oneoff-x"])


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
class _Fixture:
    """Mock ~/.claude/projects + state dir tree (matches the on-disk layout
    session_fork.py reads). Each bridge worktree dir is named after a real
    cse_ id so cse_id_from_project_dirname round-trips against it."""

    def __init__(self, tmp: Path):
        self.root = tmp
        self.projects = tmp / "projects"
        self.dev = tmp / "dev"
        self.state = tmp / "state"
        self.projects.mkdir(parents=True)
        self.dev.mkdir(parents=True)

    def make_bridge(
        self, repo: str, cse_id: str,
        *, sid: str = "uuid1", lines: list = None, mtime_offset: float = 0.0,
    ) -> Path:
        """Create a bridge worktree project dir + a transcript jsonl."""
        wt = self.dev / repo / ".claude" / "worktrees" / f"bridge-{cse_id}"
        wt.mkdir(parents=True, exist_ok=True)
        dirname = encode_project_dir(str(wt))
        proj_dir = self.projects / dirname
        proj_dir.mkdir(parents=True, exist_ok=True)
        body = lines or [{"sessionId": sid, "cwd": str(wt), "type": "user"}]
        p = proj_dir / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in body) + "\n")
        if mtime_offset:
            mt = time.time() + mtime_offset
            os.utime(p, (mt, mt))
        return proj_dir

    def mark_archived(self, proj_dir: Path) -> None:
        archived_marker_path(proj_dir).touch()

    def make_oneoff(
        self, name: str, directory: Path, *, pid: int, started_at: str,
        session_id: str = None,
    ) -> Path:
        return write_oneoff_checkpoint(
            self.state, name=name, directory=str(directory),
            pid=pid, started_at=started_at, session_id=session_id,
        )

    def make_oneoff_transcript(
        self, directory: Path, *, sid: str, mtime: float = None, body: list = None,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        proj_dir = self.projects / encode_project_dir(str(directory))
        proj_dir.mkdir(parents=True, exist_ok=True)
        lines = body or [{"sessionId": sid, "cwd": str(directory), "type": "user"}]
        p = proj_dir / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p


class _FixtureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = _Fixture(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()


# --------------------------------------------------------------------------- #
# find_orphan_candidates
# --------------------------------------------------------------------------- #
class FindOrphanCandidatesTest(_FixtureTest):
    def test_disconnected_session_picked_up(self):
        proj = self.fx.make_bridge("repoA", "cse_A1")
        cands = find_orphan_candidates(
            self.fx.projects,
            {"cse_A1": {"id": "cse_A1", "connection_status": "disconnected"}},
            now=time.time(), ttl_secs=3600,
            is_archived=lambda _d: False, is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual([c.cse_id for c in cands], ["cse_A1"])
        self.assertEqual(cands[0].project_dir, proj)

    def test_connected_session_skipped(self):
        self.fx.make_bridge("repoA", "cse_A1")
        cands = find_orphan_candidates(
            self.fx.projects,
            {"cse_A1": {"id": "cse_A1", "connection_status": "connected"}},
            now=time.time(), ttl_secs=3600,
            is_archived=lambda _d: False, is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual(cands, [])

    def test_ttl_skips_old_transcript(self):
        # AC: a 25h-old transcript is not rehydrated.
        self.fx.make_bridge("repoA", "cse_A1", mtime_offset=-25 * 3600)
        cands = find_orphan_candidates(
            self.fx.projects,
            {},  # empty live -> orphan by default
            now=time.time(), ttl_secs=24 * 3600,
            is_archived=lambda _d: False, is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual(cands, [])

    def test_archived_marker_skips(self):
        # AC: archived marker is respected.
        proj = self.fx.make_bridge("repoA", "cse_A1")
        self.fx.mark_archived(proj)
        cands = find_orphan_candidates(
            self.fx.projects, {},
            now=time.time(), ttl_secs=3600,
            is_archived=is_project_archived,
            is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual(cands, [])

    def test_already_rehydrated_skipped(self):
        self.fx.make_bridge("repoA", "cse_A1")
        cands = find_orphan_candidates(
            self.fx.projects, {},
            now=time.time(), ttl_secs=3600,
            is_archived=lambda _d: False,
            is_already_rehydrated=lambda cid: cid == "cse_A1",
        )
        self.assertEqual(cands, [])

    def test_missing_projects_root_returns_empty(self):
        cands = find_orphan_candidates(
            Path("/no/such/dir"), {}, now=0, ttl_secs=3600,
            is_archived=lambda _d: False, is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual(cands, [])

    def test_non_bridge_dirs_skipped(self):
        # A plain (non-bridge) project dir has no cse_id encoded in its name.
        (self.fx.projects / "-Users-me-dev-plain").mkdir(parents=True)
        cands = find_orphan_candidates(
            self.fx.projects, {},
            now=time.time(), ttl_secs=3600,
            is_archived=lambda _d: False, is_already_rehydrated=lambda _c: False,
        )
        self.assertEqual(cands, [])


# --------------------------------------------------------------------------- #
# rehydrate_supervisor_orphans  (end-to-end, mocks list_sessions only)
# --------------------------------------------------------------------------- #
class RehydrateSupervisorOrphansTest(_FixtureTest):
    def _run(self, sessions, **kw):
        log_lines = []
        res = rehydrate_supervisor_orphans(
            projects_root=self.fx.projects,
            state_dir=self.fx.state,
            dev=str(self.fx.dev),
            list_sessions=lambda: sessions,
            now=time.time(),
            log=log_lines.append,
            **kw,
        )
        return res, log_lines

    def test_forks_orphan_and_writes_marker(self):
        # AC: after kickstart, every prior session's transcript forked + marker.
        proj = self.fx.make_bridge("repoA", "cse_A1")
        sessions = [{"id": "cse_A1", "connection_status": "disconnected"}]
        res, _ = self._run(sessions)
        self.assertEqual(res.candidates, 1)
        self.assertEqual(res.forked, ["cse_A1"])
        marker = read_rehydrated_marker(self.fx.state, "cse_A1")
        self.assertIsNotNone(marker)
        self.assertIn("new_sid", marker)
        # Sibling transcript landed in the repo's main checkout (--into-main).
        self.assertTrue(Path(marker["sibling_path"]).exists())
        self.assertEqual(marker["run_dir"], str(self.fx.dev / "repoA"))

    def test_no_double_fork_on_second_pass(self):
        # AC: running the supervisor twice in a row does not duplicate siblings.
        self.fx.make_bridge("repoA", "cse_A1")
        sessions = [{"id": "cse_A1", "connection_status": "disconnected"}]
        r1, _ = self._run(sessions)
        r2, _ = self._run(sessions)
        self.assertEqual(r1.forked, ["cse_A1"])
        self.assertEqual(r2.candidates, 0)
        self.assertEqual(r2.forked, [])

    def test_marker_stale_resets_idempotency(self):
        # If the user deletes the sibling, next pass re-forks (so the user
        # always has *something* to pick up in /resume).
        self.fx.make_bridge("repoA", "cse_A1")
        sessions = [{"id": "cse_A1", "connection_status": "disconnected"}]
        r1, _ = self._run(sessions)
        marker = read_rehydrated_marker(self.fx.state, "cse_A1")
        Path(marker["sibling_path"]).unlink()  # user cleanup
        r2, _ = self._run(sessions)
        self.assertEqual(r2.forked, ["cse_A1"])

    def test_no_token_aborts_safely(self):
        # AC: when the API is unavailable, we don't fork blindly.
        self.fx.make_bridge("repoA", "cse_A1")
        log_lines = []
        res = rehydrate_supervisor_orphans(
            projects_root=self.fx.projects,
            state_dir=self.fx.state,
            dev=str(self.fx.dev),
            list_sessions=lambda: None,  # API failure
            now=time.time(),
            log=log_lines.append,
        )
        self.assertTrue(res.skipped_no_token)
        self.assertEqual(res.forked, [])
        self.assertTrue(any("skipped" in line for line in log_lines))

    def test_ttl_filters_old_orphans(self):
        # AC: a 25h-old transcript is not rehydrated.
        self.fx.make_bridge("repoA", "cse_A1", mtime_offset=-25 * 3600)
        sessions = [{"id": "cse_A1", "connection_status": "disconnected"}]
        res, _ = self._run(sessions, ttl_secs=24 * 3600)
        self.assertEqual(res.candidates, 0)

    def test_archived_marker_blocks_fork(self):
        # AC: archived marker is respected end-to-end.
        proj = self.fx.make_bridge("repoA", "cse_A1")
        self.fx.mark_archived(proj)
        sessions = [{"id": "cse_A1", "connection_status": "disconnected"}]
        res, _ = self._run(sessions)
        self.assertEqual(res.candidates, 0)
        self.assertIsNone(read_rehydrated_marker(self.fx.state, "cse_A1"))

    def test_mixed_orphans_and_live_sessions(self):
        self.fx.make_bridge("repoA", "cse_A1")  # orphan
        self.fx.make_bridge("repoB", "cse_B2")  # live
        sessions = [
            {"id": "cse_A1", "connection_status": "disconnected"},
            {"id": "cse_B2", "connection_status": "connected"},
        ]
        res, _ = self._run(sessions)
        self.assertEqual(res.forked, ["cse_A1"])


# --------------------------------------------------------------------------- #
# One-off checkpoint write/read/remove
# --------------------------------------------------------------------------- #
class OneoffCheckpointIoTest(_FixtureTest):
    def test_write_then_load_roundtrips(self):
        p = self.fx.make_oneoff(
            "oneoff-mini-aaa", Path("/tmp/x"), pid=4242,
            started_at="2026-05-31T12:00:00+00:00",
        )
        self.assertTrue(p.exists())
        self.assertEqual(p, oneoff_checkpoint_path(self.fx.state, "oneoff-mini-aaa"))
        cps = load_oneoff_checkpoints(self.fx.state)
        self.assertEqual(len(cps), 1)
        self.assertEqual(cps[0].pid, 4242)
        self.assertEqual(cps[0].dir, "/tmp/x")
        self.assertEqual(cps[0].name, "oneoff-mini-aaa")

    def test_remove_drops_only_named_checkpoint(self):
        self.fx.make_oneoff("oneoff-a", Path("/a"), pid=1, started_at="2026-05-31T00:00:00+00:00")
        self.fx.make_oneoff("oneoff-b", Path("/b"), pid=2, started_at="2026-05-31T00:00:00+00:00")
        self.assertTrue(remove_oneoff_checkpoint(self.fx.state, "oneoff-a"))
        names = {cp.name for cp in load_oneoff_checkpoints(self.fx.state)}
        self.assertEqual(names, {"oneoff-b"})
        # Idempotent: removing again is a no-op (returns False).
        self.assertFalse(remove_oneoff_checkpoint(self.fx.state, "oneoff-a"))

    def test_corrupt_checkpoint_skipped_not_fatal(self):
        # One bad file mustn't poison the sweep -- the rest still load.
        self.fx.make_oneoff("oneoff-good", Path("/g"), pid=1,
                            started_at="2026-05-31T00:00:00+00:00")
        bad = self.fx.state / "oneoffs" / "oneoff-bad.json"
        bad.write_text("not json")
        cps = load_oneoff_checkpoints(self.fx.state)
        self.assertEqual([c.name for c in cps], ["oneoff-good"])


# --------------------------------------------------------------------------- #
# sweep_oneoff_checkpoints (end-to-end against the fixture)
# --------------------------------------------------------------------------- #
class SweepOneoffCheckpointsTest(_FixtureTest):
    def _iso_now(self, offset: float = 0.0) -> str:
        return datetime.fromtimestamp(time.time() + offset, timezone.utc)\
            .isoformat(timespec="seconds")

    def test_alive_pid_left_alone(self):
        # A still-running one-off must not be touched.
        directory = self.fx.dev / "repoA" / "oneoff-cwd"
        self.fx.make_oneoff("oneoff-mini-aaa", directory, pid=99999,
                            started_at=self._iso_now())
        self.fx.make_oneoff_transcript(directory, sid="uuid1")
        log_lines = []
        res = sweep_oneoff_checkpoints(
            projects_root=self.fx.projects, state_dir=self.fx.state,
            dev=str(self.fx.dev),
            alive=lambda _p: True,
            now=time.time(), log=log_lines.append,
        )
        self.assertEqual(res.recovered, [])
        self.assertEqual(res.deleted_stale, [])
        # Checkpoint still on disk.
        self.assertTrue(oneoff_checkpoint_path(self.fx.state, "oneoff-mini-aaa").exists())

    def test_dead_pid_with_transcript_recovers(self):
        # AC: dead PID + transcript -> fork-and-tag, checkpoint cleaned up.
        directory = self.fx.dev / "repoA" / "oneoff-cwd"
        self.fx.make_oneoff("oneoff-mini-aaa", directory, pid=12345,
                            started_at=self._iso_now(offset=-60))
        self.fx.make_oneoff_transcript(directory, sid="uuid1")
        log_lines = []
        res = sweep_oneoff_checkpoints(
            projects_root=self.fx.projects, state_dir=self.fx.state,
            dev=str(self.fx.dev),
            alive=lambda _p: False,
            now=time.time(), log=log_lines.append,
        )
        self.assertEqual(res.recovered, ["oneoff-mini-aaa"])
        # The fork wrote a sibling jsonl in the same project dir.
        proj_dir = self.fx.projects / encode_project_dir(str(directory))
        jsonls = sorted(proj_dir.glob("*.jsonl"))
        # original + 1 sibling
        self.assertEqual(len(jsonls), 2)
        # Checkpoint cleaned up.
        self.assertFalse(oneoff_checkpoint_path(self.fx.state, "oneoff-mini-aaa").exists())

    def test_dead_pid_no_transcript_drops_checkpoint(self):
        # Spawn died before its first turn was written -- nothing to fork,
        # so just clean up the stale checkpoint.
        self.fx.make_oneoff("oneoff-mini-aaa", Path("/no/such/dir"), pid=12345,
                            started_at=self._iso_now())
        res = sweep_oneoff_checkpoints(
            projects_root=self.fx.projects, state_dir=self.fx.state,
            dev=str(self.fx.dev),
            alive=lambda _p: False,
            now=time.time(),
        )
        self.assertEqual(res.recovered, [])
        # Not counted as deleted_stale (that's the TTL path) -- it was deleted
        # via the "no transcript" branch, which is silent in the result struct.
        self.assertFalse(oneoff_checkpoint_path(self.fx.state, "oneoff-mini-aaa").exists())

    def test_dead_pid_past_ttl_is_dropped_as_stale(self):
        directory = self.fx.dev / "repoA" / "oneoff-cwd"
        # 25h ago: past the 24h default TTL.
        old_iso = (datetime.fromtimestamp(time.time() - 25 * 3600, timezone.utc)
                   .isoformat(timespec="seconds"))
        self.fx.make_oneoff("oneoff-mini-aaa", directory, pid=12345,
                            started_at=old_iso)
        self.fx.make_oneoff_transcript(directory, sid="uuid1",
                                       mtime=time.time() - 25 * 3600)
        res = sweep_oneoff_checkpoints(
            projects_root=self.fx.projects, state_dir=self.fx.state,
            dev=str(self.fx.dev),
            alive=lambda _p: False,
            now=time.time(),
            ttl_secs=24 * 3600,
        )
        self.assertEqual(res.recovered, [])
        self.assertEqual(res.deleted_stale, ["oneoff-mini-aaa"])
        self.assertFalse(oneoff_checkpoint_path(self.fx.state, "oneoff-mini-aaa").exists())

    def test_empty_state_dir_returns_empty_result(self):
        res = sweep_oneoff_checkpoints(
            projects_root=self.fx.projects, state_dir=self.fx.state,
            dev=str(self.fx.dev),
            alive=lambda _p: False, now=time.time(),
        )
        self.assertEqual(res.recovered, [])
        self.assertEqual(res.deleted_stale, [])


class FindOneoffTranscriptTest(_FixtureTest):
    def test_returns_matching_cwd(self):
        directory = self.fx.dev / "repoA" / "wd"
        self.fx.make_oneoff_transcript(directory, sid="uuid1")
        p = find_oneoff_transcript(self.fx.projects, str(directory), None)
        self.assertIsNotNone(p)
        self.assertEqual(p.stem, "uuid1")

    def test_skips_when_cwd_doesnt_match(self):
        directory = self.fx.dev / "repoA" / "wd"
        # Write a transcript that records a *different* cwd inside the same
        # project dir -- mimics an unrelated user session that happened to
        # share a project dir hash. (Synthetic but exercises the filter.)
        directory.mkdir(parents=True)
        proj_dir = self.fx.projects / encode_project_dir(str(directory))
        proj_dir.mkdir(parents=True, exist_ok=True)
        p = proj_dir / "uuidX.jsonl"
        p.write_text(json.dumps({"sessionId": "uuidX",
                                 "cwd": "/somewhere/else", "type": "user"}) + "\n")
        result = find_oneoff_transcript(self.fx.projects, str(directory), None)
        self.assertIsNone(result)

    def test_skips_transcripts_older_than_started_at(self):
        directory = self.fx.dev / "repoA" / "wd"
        self.fx.make_oneoff_transcript(directory, sid="uuid_old",
                                       mtime=time.time() - 3600)
        result = find_oneoff_transcript(
            self.fx.projects, str(directory), started_at_epoch=time.time(),
        )
        self.assertIsNone(result)


# --------------------------------------------------------------------------- #
# Marker helpers
# --------------------------------------------------------------------------- #
class IsAlreadyRehydratedTest(_FixtureTest):
    def test_returns_false_without_marker(self):
        self.assertFalse(is_already_rehydrated(self.fx.state, self.fx.projects, "cse_x"))

    def test_returns_true_when_sibling_exists(self):
        sib = self.fx.projects / "sib.jsonl"
        sib.write_text("ok")
        write_rehydrated_marker(
            self.fx.state, "cse_x",
            new_sid="NEW", source_path=Path("/src"), sibling_path=sib,
            rehydrated_at="now", run_dir="/run",
        )
        self.assertTrue(is_already_rehydrated(self.fx.state, self.fx.projects, "cse_x"))

    def test_clears_marker_when_sibling_missing(self):
        # Self-healing: a stale marker pointing at a deleted sibling is dropped
        # so the next pass re-forks.
        sib = self.fx.projects / "sib.jsonl"
        sib.write_text("ok")
        write_rehydrated_marker(
            self.fx.state, "cse_x",
            new_sid="NEW", source_path=Path("/src"), sibling_path=sib,
            rehydrated_at="now", run_dir="/run",
        )
        sib.unlink()
        self.assertFalse(is_already_rehydrated(self.fx.state, self.fx.projects, "cse_x"))
        self.assertIsNone(read_rehydrated_marker(self.fx.state, "cse_x"))


if __name__ == "__main__":
    unittest.main()
