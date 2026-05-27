"""Tests for the manager guideline eval-case I/O (remote_control.eval_cases).

Pure-I/O: no manager-ui server, no analyzer spawn -- just snapshot build +
save + load + the Action (de)serializer the runner will use later (phase 3).
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from remote_control import eval_cases as ec
from remote_control.manager import (
    ANSWER, REVIEW, Action, RequiredAction, action_sig,
)


def _ans_action(sid="cse_1", question="pick one") -> Action:
    return Action(
        session_id=sid, repo="ff", kind=ANSWER, reason="waiting on Q",
        run_dir="/d", question=question, managed=True,
        required=RequiredAction(
            tool_name="AskUserQuestion", tool_use_id="tu_1",
            description="pick",
            questions=[{"question": question, "header": "H",
                        "options": [{"label": "Yes", "description": "yes"},
                                    {"label": "No", "description": "no"}],
                        "multiSelect": False}]))


def _rev_action(sid="cse_2") -> Action:
    return Action(session_id=sid, repo="ff", kind=REVIEW, reason="idle 30m",
                  run_dir="/d", managed=True)


class CaseIdTest(unittest.TestCase):
    def test_answer_sig_becomes_file_safe_slug(self):
        # ANSWER:cse_1:f3a2b1 -> dashes, no colons (filesystem-safe).
        cid = ec.case_id_from_sig("ANSWER:cse_1:f3a2b1")
        self.assertEqual(cid, "ANSWER-cse_1-f3a2b1")

    def test_review_sig_drops_trailing_empty_qhash(self):
        # REVIEW:cse_2: (empty qhash) -> no trailing dash.
        cid = ec.case_id_from_sig("REVIEW:cse_2:")
        self.assertEqual(cid, "REVIEW-cse_2")

    def test_unsafe_chars_collapse_to_dash(self):
        cid = ec.case_id_from_sig("X:foo/bar:!!hash")
        self.assertEqual(cid, "X-foo-bar-hash")


class ActionRoundtripTest(unittest.TestCase):
    def test_answer_with_required_roundtrips(self):
        a = _ans_action()
        d = ec.action_to_jsonable(a)
        # JSON-roundtrip to make sure nothing exotic snuck in.
        d2 = json.loads(json.dumps(d))
        a2 = ec.action_from_jsonable(d2)
        self.assertEqual(a, a2)
        self.assertIsInstance(a2.required, RequiredAction)
        self.assertEqual(a2.required.questions[0]["options"][0]["label"], "Yes")

    def test_review_without_required_roundtrips(self):
        a = _rev_action()
        a2 = ec.action_from_jsonable(json.loads(json.dumps(ec.action_to_jsonable(a))))
        self.assertEqual(a, a2)
        self.assertIsNone(a2.required)


class BuildAndSaveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_build_case_includes_all_pieces(self):
        a = _ans_action()
        when = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
        case = ec.build_case(
            action=a,
            transcript_tail=[{"role": "assistant", "text": "ask"},
                             {"role": "user", "text": "answer"}],
            actual={"rec_manager": "none", "rec_session": "pick Yes",
                    "analysis": "they want Yes"},
            expected={"rec_manager": "  ", "rec_session": "pick Yes"},
            tags=["A-waiting-q"], notes="seed", now=when)
        self.assertEqual(case["schema"], ec.CASE_SCHEMA_VERSION)
        self.assertEqual(case["sig"], action_sig(a))
        self.assertEqual(case["id"], ec.case_id_from_sig(action_sig(a)))
        self.assertEqual(case["captured_at"], "2026-05-26T12:00:00+00:00")
        self.assertEqual(case["tags"], ["A-waiting-q"])
        self.assertEqual(case["notes"], "seed")
        # Expected strings are stripped; blank ones become "".
        self.assertEqual(case["expected"]["rec_manager"], "")
        self.assertEqual(case["expected"]["rec_session"], "pick Yes")
        # Action is bundled as a plain dict (not a NamedTuple array).
        self.assertEqual(case["input"]["action"]["session_id"], a.session_id)
        self.assertIsInstance(case["input"]["action"]["required"], dict)
        # Transcript tail is preserved verbatim.
        self.assertEqual(case["input"]["transcript_tail"][0]["text"], "ask")

    def test_save_writes_file_named_by_id(self):
        a = _rev_action()
        case = ec.build_case(action=a, transcript_tail=[],
                             actual={"rec_manager": "", "rec_session": "",
                                     "analysis": ""},
                             expected={"rec_manager": "", "rec_session": "defer"})
        path = ec.save_case(self.dir, case)
        self.assertEqual(path, self.dir / f"{case['id']}.json")
        self.assertTrue(path.exists())
        loaded = ec.load_case(path)
        self.assertEqual(loaded["sig"], action_sig(a))
        self.assertEqual(loaded["expected"]["rec_session"], "defer")

    def test_save_is_upsert_same_id_overwrites(self):
        # Re-saving the same situation overwrites so the operator can refine
        # an expected rec without leaving stale duplicates around.
        a = _rev_action()
        case_v1 = ec.build_case(action=a, transcript_tail=[],
                                actual={"rec_manager": "", "rec_session": "", "analysis": ""},
                                expected={"rec_manager": "", "rec_session": "v1"})
        ec.save_case(self.dir, case_v1)
        case_v2 = ec.build_case(action=a, transcript_tail=[],
                                actual={"rec_manager": "", "rec_session": "", "analysis": ""},
                                expected={"rec_manager": "", "rec_session": "v2"})
        ec.save_case(self.dir, case_v2)
        files = list(self.dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(ec.load_case(files[0])["expected"]["rec_session"], "v2")

    def test_save_creates_missing_dir(self):
        a = _rev_action()
        nested = self.dir / "deeper" / "still"
        case = ec.build_case(action=a, transcript_tail=[],
                             actual={"rec_manager": "", "rec_session": "", "analysis": ""},
                             expected={"rec_manager": "", "rec_session": ""})
        ec.save_case(nested, case)
        self.assertTrue((nested / f"{case['id']}.json").exists())


class ListCasesTest(unittest.TestCase):
    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Pointing at a non-existent subdir is a fresh-checkout situation:
            # the runner should treat it as "no cases" rather than crashing.
            self.assertEqual(ec.list_cases(Path(tmp) / "missing"), [])

    def test_list_returns_cases_sorted_by_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for sid in ("cse_z", "cse_a", "cse_m"):
                a = _rev_action(sid=sid)
                ec.save_case(d, ec.build_case(action=a, transcript_tail=[],
                                              actual={"rec_manager": "",
                                                      "rec_session": "",
                                                      "analysis": ""},
                                              expected={"rec_manager": "",
                                                        "rec_session": ""}))
            ids = [c["id"] for c in ec.list_cases(d)]
            self.assertEqual(ids, sorted(ids))


if __name__ == "__main__":
    unittest.main()
