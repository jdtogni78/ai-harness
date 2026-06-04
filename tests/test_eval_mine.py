"""Tests for the rollout miner (remote_control.eval_mine).

Pure I/O over synthetic jsonl fixtures: builds a minimal AskUserQuestion +
tool_result pair, runs the miner against a temp src dir, asserts the
upserted case files match what build_case would have produced. No live
~/.claude/projects scan.
"""
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from remote_control import eval_mine
from remote_control.eval_cases import load_case


# --------------------------------------------------------------------------- #
# jsonl-line builders for the rollout shape
# --------------------------------------------------------------------------- #
def _asst_ask(
    *,
    tool_use_id: str = "toolu_1",
    questions: Optional[List[dict]] = None,
    sess_uuid: str = "11111111-2222-3333-4444-555566667777",
    cwd: str = "/Users/me/dev/ai-harness/.claude/worktrees/bridge-cse_AAA",
    asst_text: str = "Need your call on this.",
) -> dict:
    if questions is None:
        questions = [{
            "question": "Pick one?",
            "header": "H",
            "multiSelect": False,
            "options": [
                {"label": "A", "description": "do A"},
                {"label": "B", "description": "do B"},
            ],
        }]
    return {
        "type": "assistant",
        "sessionId": sess_uuid,
        "cwd": cwd,
        "gitBranch": "main",
        "message": {
            "content": [
                {"type": "text", "text": asst_text},
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "AskUserQuestion",
                    "input": {"questions": questions},
                },
            ],
        },
    }


def _user_answer(
    *,
    tool_use_id: str = "toolu_1",
    answers: Optional[Dict[str, Any]] = None,
    questions_echo: Optional[List[dict]] = None,
    sess_uuid: str = "11111111-2222-3333-4444-555566667777",
    cwd: str = "/Users/me/dev/ai-harness/.claude/worktrees/bridge-cse_AAA",
    content_str: str = "User answered.",
) -> dict:
    if answers is None:
        answers = {"Pick one?": "A"}
    return {
        "type": "user",
        "sessionId": sess_uuid,
        "cwd": cwd,
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content_str,
            }],
        },
        "toolUseResult": {
            "questions": questions_echo or [],
            "answers": answers,
        },
    }


def _write_jsonl(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _src_with_file(tmp: Path, slug: str, fname: str,
                   records: List[dict]) -> Path:
    """Drop one rollout file at tmp/src/<slug>/<fname> and return src."""
    src = tmp / "src"
    _write_jsonl(src / slug / fname, records)
    return src


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
class HappyPathTest(unittest.TestCase):
    def test_single_pair_writes_one_case(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness",
                "11111111-2222-3333-4444-555566667777.jsonl",
                [_asst_ask(), _user_answer()],
            )
            out = tmp / "out"
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=out, stdout=buf)
            self.assertTrue(res["ok"])
            self.assertEqual(res["found"], 1)
            self.assertEqual(res["wrote"], 1)
            self.assertEqual(res["updated"], 0)
            files = sorted(out.glob("*.json"))
            self.assertEqual(len(files), 1)
            case = load_case(files[0])
            self.assertEqual(case["captured_by"], "mined")
            self.assertIn("A-waiting-q", case["tags"])
            self.assertIn("mined", case["tags"])
            action = case["input"]["action"]
            # session_id namespacing isolates mined rollouts from live cse_*.
            self.assertTrue(action["session_id"].startswith("rollout-"))
            self.assertEqual(action["repo"], "ai-harness")
            # The Action carries the canonical lowercase kind so action_sig()
            # hashes the question (and the case id ends up unique per Q).
            self.assertEqual(action["kind"], "answer")
            self.assertEqual(action["required"]["tool_name"], "AskUserQuestion")
            self.assertEqual(action["required"]["tool_use_id"], "toolu_1")
            self.assertEqual(case["expected"]["rec_session"], "A")
            self.assertEqual(case["expected"]["rec_manager"], "none")
            self.assertEqual(case["actual_at_capture"]["rec_session"], "")


class MultiQuestionTest(unittest.TestCase):
    def test_two_questions_joined_in_question_order(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            qs = [
                {"question": "Q1?", "header": "Q1", "multiSelect": False,
                 "options": [{"label": "A1", "description": ""},
                             {"label": "B1", "description": ""}]},
                {"question": "Q2?", "header": "Q2", "multiSelect": False,
                 "options": [{"label": "A2", "description": ""},
                             {"label": "B2", "description": ""}]},
            ]
            answers = {"Q2?": "B2", "Q1?": "A1"}  # insertion order shuffled
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness",
                "abc.jsonl",
                [_asst_ask(questions=qs), _user_answer(answers=answers)],
            )
            buf = io.StringIO()
            eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            case = load_case(next((tmp / "out").glob("*.json")))
            # In question order: Q1 first, then Q2.
            self.assertEqual(case["expected"]["rec_session"], "A1, B2")


class MultiSelectTest(unittest.TestCase):
    def test_list_value_is_comma_joined(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            qs = [{
                "question": "Which?", "header": "W", "multiSelect": True,
                "options": [{"label": "X", "description": ""},
                            {"label": "Y", "description": ""},
                            {"label": "Z", "description": ""}],
            }]
            answers = {"Which?": ["X", "Z"]}
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness",
                "abc.jsonl",
                [_asst_ask(questions=qs), _user_answer(answers=answers)],
            )
            buf = io.StringIO()
            eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            case = load_case(next((tmp / "out").glob("*.json")))
            self.assertEqual(case["expected"]["rec_session"], "X, Z")

    def test_already_comma_joined_string_passes_through(self):
        """Some rollouts encode multi-select as the comma-joined string already."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            qs = [{"question": "Which?", "header": "W", "multiSelect": True,
                   "options": [{"label": "X", "description": ""}]}]
            answers = {"Which?": "X, Y"}
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness",
                "abc.jsonl",
                [_asst_ask(questions=qs), _user_answer(answers=answers)],
            )
            buf = io.StringIO()
            eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            case = load_case(next((tmp / "out").glob("*.json")))
            self.assertEqual(case["expected"]["rec_session"], "X, Y")


class MalformedSkippedTest(unittest.TestCase):
    def test_no_matching_tool_result_skips(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # AskUserQuestion never gets its result (rollout cut short).
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness", "abc.jsonl",
                [_asst_ask(tool_use_id="toolu_unanswered")],
            )
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            self.assertEqual(res["found"], 0)
            self.assertEqual(res["wrote"], 0)
            self.assertEqual(list((tmp / "out").glob("*.json")), [])

    def test_tool_result_without_answers_skipped(self):
        """A tool_result that's an error or carries no `answers` map is dropped
        -- mining only wants pairs that a human actually resolved."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            user_no_answers = _user_answer(answers={})
            user_no_answers["toolUseResult"] = {"questions": []}  # no answers key
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness", "abc.jsonl",
                [_asst_ask(), user_no_answers],
            )
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            self.assertEqual(res["wrote"], 0)
            self.assertEqual(list((tmp / "out").glob("*.json")), [])

    def test_malformed_jsonl_line_tolerated(self):
        """A garbage line in the middle of the rollout shouldn't abort the file."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src"
            (src / "-Users-me-dev-ai-harness").mkdir(parents=True)
            f = src / "-Users-me-dev-ai-harness" / "abc.jsonl"
            f.write_text(
                json.dumps(_asst_ask()) + "\n"
                + "{not json\n"
                + json.dumps(_user_answer()) + "\n"
            )
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=tmp / "out", stdout=buf)
            self.assertEqual(res["wrote"], 1)


class UpsertTest(unittest.TestCase):
    def test_second_run_updates_existing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness", "abc.jsonl",
                [_asst_ask(), _user_answer()],
            )
            out = tmp / "out"
            buf = io.StringIO()
            res1 = eval_mine.run_mine(src=src, out=out, stdout=buf)
            self.assertEqual(res1["wrote"], 1)
            files_after_first = sorted(out.glob("*.json"))
            self.assertEqual(len(files_after_first), 1)

            res2 = eval_mine.run_mine(src=src, out=out, stdout=buf)
            self.assertEqual(res2["updated"], 1)
            self.assertEqual(res2["wrote"], 0)
            files_after_second = sorted(out.glob("*.json"))
            self.assertEqual(files_after_first, files_after_second)


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness", "abc.jsonl",
                [_asst_ask(), _user_answer()],
            )
            out = tmp / "out"
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=out, dry_run=True, stdout=buf)
            self.assertEqual(res["found"], 1)
            self.assertEqual(res["wrote"], 1)  # would-write counter
            self.assertFalse(out.exists())  # nothing touched
            self.assertIn("dry-run", buf.getvalue())


class LimitAndUnknownKindTest(unittest.TestCase):
    def test_limit_caps_output_but_still_counts_found(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Three independent pairs across one file (different tool_use_ids
            # + different questions so action_sig hashes them apart).
            recs: List[dict] = []
            for n in range(3):
                tuid = f"toolu_{n}"
                qs = [{"question": f"Q{n}?", "header": "H",
                       "multiSelect": False,
                       "options": [{"label": "A", "description": ""}]}]
                recs.append(_asst_ask(tool_use_id=tuid, questions=qs))
                recs.append(_user_answer(tool_use_id=tuid,
                                         answers={f"Q{n}?": "A"}))
            src = _src_with_file(
                tmp, "-Users-me-dev-ai-harness", "abc.jsonl", recs,
            )
            buf = io.StringIO()
            res = eval_mine.run_mine(src=src, out=tmp / "out", limit=2,
                                     stdout=buf)
            self.assertEqual(res["found"], 3)
            self.assertEqual(res["wrote"], 2)
            self.assertEqual(res["skipped"], 1)
            self.assertEqual(len(list((tmp / "out").glob("*.json"))), 2)

    def test_unsupported_kind_errors(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            buf = io.StringIO()
            res = eval_mine.run_mine(src=tmp / "src", out=tmp / "out",
                                     kind="RESCUE", stdout=buf)
            self.assertFalse(res.get("ok"))


class RepoAndSessionDerivationTest(unittest.TestCase):
    def test_repo_from_cwd_handles_plain_and_worktree_paths(self):
        self.assertEqual(eval_mine._repo_from_cwd(
            "/Users/u/dev/ai-harness"), "ai-harness")
        self.assertEqual(eval_mine._repo_from_cwd(
            "/Users/u/dev/FamilyFund/.claude/worktrees/bridge-cse_X"),
            "FamilyFund")
        self.assertIsNone(eval_mine._repo_from_cwd("/tmp/random"))
        self.assertIsNone(eval_mine._repo_from_cwd(None))

    def test_session_id_namespaced_to_rollout(self):
        rec = {"sessionId": "abcdef12-3456-7890-aaaa-bbbbccccdddd"}
        sid = eval_mine._session_id_for(rec, Path("ignored.jsonl"))
        self.assertEqual(sid, "rollout-abcdef12")

    def test_transcript_tail_skips_meta_and_tool_only_turns(self):
        parsed = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "hello"}]}},
            {"type": "assistant", "isMeta": True, "message": {"content": [
                {"type": "text", "text": "meta noise"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "x", "name": "T"}]}},  # tool-only
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "<command-name>foo"}]}},  # wrapper
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "world"}]}},
        ]
        tail = eval_mine._transcript_tail(parsed, len(parsed), n=6)
        self.assertEqual(
            tail,
            [{"role": "assistant", "text": "hello"},
             {"role": "assistant", "text": "world"}],
        )


if __name__ == "__main__":
    unittest.main()
