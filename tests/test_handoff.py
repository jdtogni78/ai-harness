"""Unit tests for :mod:`remote_control.handoff`.

Coverage maps to the AC checklist on issue #31:

  * brief derivation (cwd + branch + last N user turns + 1-line preamble,
    8KB hard cap)
  * idempotency (forked uuid already in a handoff record -> skip)
  * RESUME_ON_RESTART env knob (handoff | off)
  * dispatch loop with an injected spawn function (the sessions-submit
    network call is the real spawn's job; tests never POST)
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from remote_control import handoff
from remote_control.handoff import (
    DEFAULT_MAX_BRIEF_BYTES,
    HandoffCandidate,
    candidates_from_rehydrate_markers,
    derive_brief_from_transcript,
    dispatch_handoffs,
    extract_user_turns,
    first_git_branch,
    forked_uuids_already_dispatched,
    format_brief,
    handoff_enabled,
    handoff_record_path,
    load_handoff_records,
    write_handoff_record,
)


# --------------------------------------------------------------------------- #
# Brief derivation: pure helpers
# --------------------------------------------------------------------------- #
def _user_line(text, *, is_meta=False):
    """Build one transcript line matching the on-disk schema."""
    obj = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "sessionId": "old", "cwd": "/x",
    }
    if is_meta:
        obj["isMeta"] = True
    return json.dumps(obj)


def _assistant_line(text):
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": text},
    })


class ExtractUserTurnsTest(unittest.TestCase):
    def test_string_content(self):
        lines = [_user_line("hello world")]
        self.assertEqual(extract_user_turns(lines), ["hello world"])

    def test_list_content_text_blocks(self):
        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "part two"},
            ]},
        })
        self.assertEqual(extract_user_turns([line]), ["part one part two"])

    def test_filters_assistant_turns(self):
        self.assertEqual(
            extract_user_turns([_user_line("u1"), _assistant_line("a1"), _user_line("u2")]),
            ["u1", "u2"],
        )

    def test_filters_meta_wrappers(self):
        lines = [_user_line("real"), _user_line("meta", is_meta=True)]
        self.assertEqual(extract_user_turns(lines), ["real"])

    def test_filters_command_wrappers(self):
        # `<command-name>`-style scaffolding the user didn't actually type.
        lines = [_user_line("<command-name>/foo</command-name>"), _user_line("real msg")]
        self.assertEqual(extract_user_turns(lines), ["real msg"])

    def test_skips_tool_result_only_turns(self):
        # No text blocks -> no extractable text -> skipped.
        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "..."},
            ]},
        })
        self.assertEqual(extract_user_turns([line]), [])

    def test_tolerates_blank_and_garbage_lines(self):
        lines = ["", "not json", _user_line("good"), "{}", "  "]
        self.assertEqual(extract_user_turns(lines), ["good"])

    def test_preserves_order(self):
        lines = [_user_line(f"t{i}") for i in range(6)]
        self.assertEqual(extract_user_turns(lines), [f"t{i}" for i in range(6)])


class FirstGitBranchTest(unittest.TestCase):
    def test_returns_first_non_empty(self):
        lines = [
            json.dumps({"type": "user", "gitBranch": ""}),
            json.dumps({"type": "user", "gitBranch": "main"}),
            json.dumps({"type": "user", "gitBranch": "feature/x"}),
        ]
        self.assertEqual(first_git_branch(lines), "main")

    def test_returns_none_when_missing(self):
        lines = [json.dumps({"type": "user"})]
        self.assertIsNone(first_git_branch(lines))

    def test_tolerates_garbage(self):
        self.assertIsNone(first_git_branch(["", "garbage", "{}"]))


class FormatBriefTest(unittest.TestCase):
    def test_includes_header_cwd_branch_and_turns(self):
        brief = format_brief(
            cwd="/d/proj", branch="feature/x",
            user_turns=["first", "second"],
            transcript_path="/p/x.jsonl",
        )
        # Spec'd 1-line preamble.
        self.assertIn("fresh session reconstructed from a prior unresumable thread",
                      brief.splitlines()[0])
        # Transcript path, cwd, branch surface.
        self.assertIn("/p/x.jsonl", brief)
        self.assertIn("cwd: /d/proj", brief)
        self.assertIn("branch: feature/x", brief)
        # Turns are raw-quoted and labeled.
        self.assertIn("--- turn 1 ---", brief)
        self.assertIn("first", brief)
        self.assertIn("--- turn 2 ---", brief)
        self.assertIn("second", brief)

    def test_unknown_branch_renders_placeholder(self):
        brief = format_brief(cwd="/d", branch=None, user_turns=["t"],
                             transcript_path="/p")
        self.assertIn("branch: <unknown>", brief)

    def test_no_user_turns_keeps_header_with_note(self):
        brief = format_brief(cwd="/d", branch="main", user_turns=[],
                             transcript_path="/p")
        self.assertIn("(no user turns recovered", brief)

    def test_8kb_hard_cap_drops_oldest_turns(self):
        # 5 large turns, each ~3000 chars; max=8KB forces dropping the oldest.
        turns = [f"turn{i}: " + ("x" * 3000) for i in range(5)]
        brief = format_brief(
            cwd="/d", branch="main", user_turns=turns,
            transcript_path="/p", max_bytes=8 * 1024,
        )
        self.assertLessEqual(len(brief.encode("utf-8")), 8 * 1024)
        # Newest turn survives; the oldest one gets dropped.
        self.assertIn("turn4:", brief)
        self.assertNotIn("turn0:", brief)

    def test_default_cap_is_8kb(self):
        self.assertEqual(DEFAULT_MAX_BRIEF_BYTES, 8 * 1024)


class DeriveBriefFromTranscriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, lines):
        p = self.root / name
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_takes_last_n_user_turns(self):
        lines = [json.dumps({"type": "user", "gitBranch": "main",
                             "message": {"role": "user", "content": f"t{i}"}})
                 for i in range(10)]
        p = self._write("x.jsonl", lines)
        brief = derive_brief_from_transcript(p, cwd="/d", max_turns=3)
        self.assertIn("Last 3 user turn(s)", brief)
        self.assertIn("t7", brief)
        self.assertIn("t8", brief)
        self.assertIn("t9", brief)
        self.assertNotIn("t6", brief)

    def test_includes_branch_from_transcript(self):
        lines = [json.dumps({"type": "user", "gitBranch": "feature/abc",
                             "message": {"role": "user", "content": "hi"}})]
        p = self._write("x.jsonl", lines)
        brief = derive_brief_from_transcript(p, cwd="/d")
        self.assertIn("branch: feature/abc", brief)

    def test_missing_transcript_returns_header_only(self):
        # No I/O failure -- gracefully renders a header-only brief so the new
        # session at least knows where the prior transcript was supposed to be.
        brief = derive_brief_from_transcript(
            self.root / "does-not-exist.jsonl", cwd="/d",
        )
        self.assertIn("fresh session reconstructed", brief)
        self.assertIn("/does-not-exist.jsonl", brief)
        self.assertIn("(no user turns recovered", brief)


# --------------------------------------------------------------------------- #
# Idempotency: handoff records
# --------------------------------------------------------------------------- #
class HandoffRecordIoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_load_roundtrips(self):
        write_handoff_record(
            self.state, new_cse="cse_NEW", forked_uuid="uuid-1",
            source_cse="cse_OLD", cwd="/d", dispatched_at="2026-05-31T12:00:00+00:00",
        )
        records = load_handoff_records(self.state)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["new_cse"], "cse_NEW")
        self.assertEqual(r["forked_uuid"], "uuid-1")
        self.assertEqual(r["source_cse"], "cse_OLD")

    def test_record_path_under_state_dir(self):
        p = handoff_record_path(self.state, "cse_X")
        self.assertEqual(p, self.state / "handoffs" / "cse_X.json")

    def test_load_handles_missing_dir(self):
        self.assertEqual(load_handoff_records(self.state), [])

    def test_load_skips_corrupt_files(self):
        (self.state / "handoffs").mkdir(parents=True)
        (self.state / "handoffs" / "bad.json").write_text("not json")
        write_handoff_record(
            self.state, new_cse="cse_GOOD", forked_uuid="u",
            source_cse="cse_OLD", cwd="/d", dispatched_at="now",
        )
        names = [r["new_cse"] for r in load_handoff_records(self.state)]
        self.assertEqual(names, ["cse_GOOD"])

    def test_forked_uuids_already_dispatched_returns_set(self):
        write_handoff_record(
            self.state, new_cse="cse_A", forked_uuid="uuid-1",
            source_cse="cse_OA", cwd="/d", dispatched_at="t",
        )
        write_handoff_record(
            self.state, new_cse="cse_B", forked_uuid="uuid-2",
            source_cse="cse_OB", cwd="/d", dispatched_at="t",
        )
        self.assertEqual(forked_uuids_already_dispatched(self.state),
                         {"uuid-1", "uuid-2"})

    def test_forked_uuids_empty_when_no_records(self):
        self.assertEqual(forked_uuids_already_dispatched(self.state), set())


# --------------------------------------------------------------------------- #
# Env knob
# --------------------------------------------------------------------------- #
class HandoffEnabledTest(unittest.TestCase):
    def test_default_is_enabled(self):
        self.assertTrue(handoff_enabled({}))

    def test_explicit_handoff_is_enabled(self):
        self.assertTrue(handoff_enabled({"RESUME_ON_RESTART": "handoff"}))

    def test_off_disables(self):
        self.assertFalse(handoff_enabled({"RESUME_ON_RESTART": "off"}))

    def test_case_and_whitespace_tolerant(self):
        self.assertFalse(handoff_enabled({"RESUME_ON_RESTART": "  OFF  "}))
        self.assertTrue(handoff_enabled({"RESUME_ON_RESTART": "HANDOFF"}))

    def test_unknown_value_falls_through_to_enabled(self):
        # Safer-but-noisier default; spec says "default handoff".
        self.assertTrue(handoff_enabled({"RESUME_ON_RESTART": "maybe"}))


# --------------------------------------------------------------------------- #
# candidates_from_rehydrate_markers
# --------------------------------------------------------------------------- #
class CandidatesFromMarkersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        (self.state / "rehydrated").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _marker(self, cse_id, **kw):
        (self.state / "rehydrated" / f"{cse_id}.json").write_text(json.dumps(kw))

    def test_builds_candidate_from_marker(self):
        self._marker(
            "cse_A1", new_sid="uuid-1", run_dir="/d/repoA",
            source_path="/p/old.jsonl", sibling_path="/p/new.jsonl",
        )
        cands = candidates_from_rehydrate_markers(self.state, ["cse_A1"])
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.source_cse, "cse_A1")
        self.assertEqual(c.forked_uuid, "uuid-1")
        self.assertEqual(c.run_dir, "/d/repoA")
        self.assertEqual(c.source_path, Path("/p/old.jsonl"))

    def test_skips_missing_marker(self):
        self.assertEqual(candidates_from_rehydrate_markers(self.state, ["cse_X"]), [])

    def test_skips_marker_with_missing_required_fields(self):
        # An older-format marker without run_dir can't be dispatched.
        self._marker("cse_A1", new_sid="uuid-1", source_path="/p")
        self.assertEqual(candidates_from_rehydrate_markers(self.state, ["cse_A1"]), [])


# --------------------------------------------------------------------------- #
# dispatch_handoffs  (end-to-end with an injected spawn)
# --------------------------------------------------------------------------- #
class DispatchHandoffsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        # The brief derivation reads the source transcript -- give it one.
        self.transcript = self.state / "src.jsonl"
        self.transcript.write_text(
            json.dumps({"type": "user", "gitBranch": "main",
                        "message": {"role": "user", "content": "hi"}}) + "\n"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _candidate(self, source_cse="cse_OLD", forked_uuid="uuid-1",
                   run_dir="/d/repoA"):
        return HandoffCandidate(
            source_cse=source_cse, forked_uuid=forked_uuid,
            run_dir=run_dir, source_path=self.transcript,
        )

    def test_dispatches_and_writes_record(self):
        # The spawn function NEVER hits the network -- it's a pure mock that
        # returns a synthetic new cse_*. The whole sessions-submit call is
        # the responsibility of the real spawn function in production.
        seen = []
        def fake_spawn(c, brief):
            seen.append((c.source_cse, c.forked_uuid, len(brief)))
            return f"cse_NEW_{c.source_cse[-2:]}"
        res = dispatch_handoffs(
            candidates=[self._candidate()],
            state_dir=self.state, spawn=fake_spawn,
            now_iso="2026-05-31T00:00:00+00:00",
        )
        self.assertEqual(len(res.dispatched), 1)
        self.assertEqual(res.dispatched[0]["source_cse"], "cse_OLD")
        self.assertEqual(res.dispatched[0]["new_cse"], "cse_NEW_LD")
        self.assertEqual(res.dispatched[0]["forked_uuid"], "uuid-1")
        # Brief was passed to spawn with non-zero size.
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0][2], 0)
        # Idempotency ledger now reflects it.
        self.assertIn("uuid-1", forked_uuids_already_dispatched(self.state))

    def test_skips_candidate_with_existing_handoff_record(self):
        # AC: a handoff-tagged session is not re-handoff'd on the next pass.
        write_handoff_record(
            self.state, new_cse="cse_PRIOR", forked_uuid="uuid-1",
            source_cse="cse_OLD", cwd="/d/repoA", dispatched_at="earlier",
        )
        called = []
        def fake_spawn(c, brief):
            called.append(c.source_cse)
            return "cse_X"
        res = dispatch_handoffs(
            candidates=[self._candidate()],
            state_dir=self.state, spawn=fake_spawn,
            now_iso="now",
        )
        self.assertEqual(res.dispatched, [])
        self.assertEqual(res.skipped_already_dispatched, ["uuid-1"])
        self.assertEqual(called, [])  # spawn never ran

    def test_spawn_returning_none_is_recorded_as_error(self):
        def fake_spawn(c, brief):
            return None  # e.g. cse_* registration timed out
        res = dispatch_handoffs(
            candidates=[self._candidate()],
            state_dir=self.state, spawn=fake_spawn,
            now_iso="now",
        )
        self.assertEqual(res.dispatched, [])
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.errors[0][0], "cse_OLD")
        # No record written on failure.
        self.assertEqual(forked_uuids_already_dispatched(self.state), set())

    def test_spawn_exception_does_not_abort_pass(self):
        # One bad spawn shouldn't poison the rest.
        def fake_spawn(c, brief):
            if c.source_cse == "cse_BAD":
                raise RuntimeError("boom")
            return f"cse_NEW_{c.source_cse[-2:]}"
        candidates = [
            self._candidate(source_cse="cse_BAD", forked_uuid="uuid-bad"),
            self._candidate(source_cse="cse_OK", forked_uuid="uuid-ok"),
        ]
        res = dispatch_handoffs(
            candidates=candidates,
            state_dir=self.state, spawn=fake_spawn,
            now_iso="now",
        )
        self.assertEqual(len(res.dispatched), 1)
        self.assertEqual(res.dispatched[0]["source_cse"], "cse_OK")
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.errors[0][0], "cse_BAD")
        self.assertIn("RuntimeError", res.errors[0][1])

    def test_passes_brief_with_user_turns_from_transcript(self):
        # The brief the dispatcher hands to spawn is derived from
        # source_path -- verify the actual user-turn text is in there.
        self.transcript.write_text(
            json.dumps({"type": "user", "gitBranch": "feature/y",
                        "message": {"role": "user",
                                    "content": "do the thing"}}) + "\n"
        )
        captured = {}
        def fake_spawn(c, brief):
            captured["brief"] = brief
            return "cse_NEW"
        dispatch_handoffs(
            candidates=[self._candidate()],
            state_dir=self.state, spawn=fake_spawn,
            now_iso="now",
        )
        self.assertIn("do the thing", captured["brief"])
        self.assertIn("branch: feature/y", captured["brief"])
        self.assertIn("/d/repoA", captured["brief"])  # cwd

    def test_brief_is_capped_at_8kb(self):
        big = ("x" * 5000)
        self.transcript.write_text("\n".join(
            json.dumps({"type": "user",
                        "message": {"role": "user", "content": f"{i}:" + big}})
            for i in range(5)
        ) + "\n")
        captured = {}
        def fake_spawn(c, brief):
            captured["brief"] = brief
            return "cse_NEW"
        dispatch_handoffs(
            candidates=[self._candidate()],
            state_dir=self.state, spawn=fake_spawn,
            now_iso="now",
        )
        self.assertLessEqual(len(captured["brief"].encode("utf-8")), 8 * 1024)


# --------------------------------------------------------------------------- #
# run_handoff_dispatch  (honors the env knob)
# --------------------------------------------------------------------------- #
class _FakeCfg:
    """Minimal SupervisorConfig stand-in for run_handoff_dispatch tests."""
    def __init__(self, state_dir):
        self.state_dir = state_dir


class RunHandoffDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        (self.state / "rehydrated").mkdir(parents=True)
        (self.state / "rehydrated" / "cse_A1.json").write_text(json.dumps({
            "cse_id": "cse_A1", "new_sid": "uuid-1",
            "run_dir": "/d/repoA",
            "source_path": str(self.state / "src.jsonl"),
            "sibling_path": "/p/new.jsonl",
        }))
        (self.state / "src.jsonl").write_text(json.dumps({
            "type": "user", "gitBranch": "main",
            "message": {"role": "user", "content": "hi"},
        }) + "\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_off_env_short_circuits(self):
        # AC: RESUME_ON_RESTART=off disables dispatch.
        called = []
        def fake_spawn(c, brief):
            called.append(c.source_cse); return "cse_X"
        res = handoff.run_handoff_dispatch(
            cfg=_FakeCfg(self.state), forked_cse_ids=["cse_A1"],
            env={"RESUME_ON_RESTART": "off"},
            now_iso="now", spawn=fake_spawn,
        )
        self.assertTrue(res.disabled)
        self.assertEqual(called, [])
        # No record written when disabled.
        self.assertEqual(forked_uuids_already_dispatched(self.state), set())

    def test_default_env_dispatches(self):
        captured = []
        def fake_spawn(c, brief):
            captured.append(c.source_cse); return f"cse_NEW_{c.source_cse[-2:]}"
        res = handoff.run_handoff_dispatch(
            cfg=_FakeCfg(self.state), forked_cse_ids=["cse_A1"],
            env={},  # default
            now_iso="now", spawn=fake_spawn,
        )
        self.assertFalse(res.disabled)
        self.assertEqual(captured, ["cse_A1"])
        self.assertEqual(len(res.dispatched), 1)

    def test_empty_forked_list_no_op(self):
        res = handoff.run_handoff_dispatch(
            cfg=_FakeCfg(self.state), forked_cse_ids=[],
            env={}, now_iso="now",
            spawn=lambda c, b: "cse_NEVER",
        )
        self.assertFalse(res.disabled)
        self.assertEqual(res.dispatched, [])


if __name__ == "__main__":
    unittest.main()
