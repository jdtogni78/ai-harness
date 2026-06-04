"""Tests for the manager guideline eval runner (remote_control.eval).

No real LLM calls -- the judge and the analyzer are both injected as
deterministic fakes via :func:`eval.run_eval`'s ``judges_factory`` /
``analyzer`` seams. The subprocess-judge wrappers
(:func:`eval.judge_claude` / :func:`eval.judge_codex`) get their own focused
mock to confirm the argv they build.
"""
import json
import statistics
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

from remote_control import eval as ev
from remote_control import eval_cases as ec
from remote_control.config import ManagerConfig
from remote_control.manager import REVIEW, Action


BASE_ENV = {
    "REMOTE_CONTROL_HOST": "testhost",
    "REMOTE_CONTROL_DEV": "/Users/x/dev",
    "REMOTE_CONTROL_CLAUDE_BIN": "/bin/claude",
}


def _cfg(**over) -> ManagerConfig:
    env = dict(BASE_ENV)
    env.update({k: str(v) for k, v in over.items()})
    return ManagerConfig.from_env(env)


def _rev_action(sid="cse_test") -> Action:
    return Action(session_id=sid, repo="ah", kind=REVIEW,
                  reason="idle 30m", run_dir="/d", managed=True)


def _seed_case(dir_: Path, *, sid="cse_test",
               expected_manager="archive when done",
               expected_session="none") -> Dict[str, Any]:
    a = _rev_action(sid=sid)
    case = ec.build_case(
        action=a,
        transcript_tail=[{"role": "assistant", "text": "shipped"},
                         {"role": "user", "text": "great"}],
        actual={"rec_manager": "", "rec_session": "", "analysis": ""},
        expected={"rec_manager": expected_manager,
                  "rec_session": expected_session},
        tags=["B-idle"], notes="seed")
    ec.save_case(dir_, case)
    return case


def _good_actual(actual_manager="archive when done",
                 actual_session="none") -> Dict[str, Any]:
    return {"ok": True, "rec_manager": actual_manager,
            "rec_session": actual_session,
            "recommendation": f"MANAGER: {actual_manager}",
            "analysis": "looks done", "note": "analyzed", "raw": "..."}


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
class HashingTest(unittest.TestCase):
    def test_actual_hash_ignores_analysis_prose(self):
        # The analyzer's wording varies run-to-run; only the rec halves
        # belong in the cache key, so a different analysis must NOT bust
        # the cache.
        a = {"rec_manager": "archive", "rec_session": "none",
             "analysis": "version one"}
        b = {"rec_manager": "archive", "rec_session": "none",
             "analysis": "completely different sentence"}
        self.assertEqual(ev.actual_hash(a), ev.actual_hash(b))

    def test_actual_hash_changes_when_rec_changes(self):
        a = {"rec_manager": "archive", "rec_session": "none"}
        b = {"rec_manager": "fork+resume", "rec_session": "none"}
        self.assertNotEqual(ev.actual_hash(a), ev.actual_hash(b))

    def test_actual_hash_is_whitespace_stable(self):
        a = {"rec_manager": "  archive  ", "rec_session": "none"}
        b = {"rec_manager": "archive", "rec_session": "none"}
        self.assertEqual(ev.actual_hash(a), ev.actual_hash(b))

    def test_guidelines_hash_missing_file_is_empty_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.md"
            self.assertEqual(ev.guidelines_hash_of(missing),
                             ev.sha256_text(""))


# --------------------------------------------------------------------------- #
# Judge output parser
# --------------------------------------------------------------------------- #
class ParseJudgeOutputTest(unittest.TestCase):
    def test_plain_json_line(self):
        out = ev.parse_judge_output(
            '{"verdict":"equivalent","score":4,"reason":"same intent"}')
        self.assertEqual(out["verdict"], "equivalent")
        self.assertEqual(out["score"], 4)
        self.assertEqual(out["reason"], "same intent")
        self.assertNotIn("error", out)

    def test_json_with_preamble_and_fence(self):
        text = ('here is my evaluation:\n```json\n'
                '{"verdict": "worse", "score": 2, "reason": "wrong op"}\n'
                '```\nthanks')
        out = ev.parse_judge_output(text)
        self.assertEqual(out["verdict"], "worse")
        self.assertEqual(out["score"], 2)

    def test_score_clamped_to_1_5(self):
        out = ev.parse_judge_output(
            '{"verdict":"better","score":99,"reason":"x"}')
        self.assertEqual(out["score"], 5)
        out = ev.parse_judge_output(
            '{"verdict":"worse","score":-3,"reason":"x"}')
        self.assertEqual(out["score"], 1)

    def test_empty_output_is_low_score_with_error(self):
        out = ev.parse_judge_output("")
        self.assertEqual(out["score"], 1)
        self.assertEqual(out["verdict"], "worse")
        self.assertEqual(out["error"], "empty")

    def test_unknown_verdict_is_error(self):
        out = ev.parse_judge_output(
            '{"verdict":"maybe","score":4,"reason":"x"}')
        self.assertEqual(out["error"], "verdict")
        self.assertEqual(out["score"], 1)

    def test_unparseable_marks_parse_error(self):
        out = ev.parse_judge_output("hello there, no JSON anywhere")
        self.assertEqual(out["error"], "parse")
        self.assertEqual(out["score"], 1)

    def test_picks_object_containing_verdict(self):
        # A judge that produces multiple JSON-like blobs (only one has
        # "verdict") -- we must grab the right one.
        text = ('{"meta":1} more text {"verdict":"equivalent",'
                '"score":5,"reason":"ok"}')
        out = ev.parse_judge_output(text)
        self.assertEqual(out["verdict"], "equivalent")
        self.assertEqual(out["score"], 5)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
class AggregateSamplesTest(unittest.TestCase):
    def test_n3_mean_and_stdev_match_statistics(self):
        samples = [
            {"verdict": "equivalent", "score": 4, "reason": "a"},
            {"verdict": "equivalent", "score": 5, "reason": "b"},
            {"verdict": "equivalent", "score": 3, "reason": "c"},
        ]
        agg = ev.aggregate_samples(samples)
        self.assertEqual(agg["n"], 3)
        self.assertAlmostEqual(agg["mean"], 4.0, places=3)
        self.assertAlmostEqual(
            agg["stdev"], round(statistics.pstdev([4, 5, 3]), 3), places=3)
        self.assertEqual(agg["verdict_mode"], "equivalent")

    def test_n1_stdev_is_zero_not_error(self):
        # statistics.pstdev raises on n=1; the aggregator must guard.
        agg = ev.aggregate_samples(
            [{"verdict": "better", "score": 5, "reason": "x"}])
        self.assertEqual(agg["stdev"], 0.0)
        self.assertEqual(agg["mean"], 5.0)

    def test_verdict_mode_picks_majority(self):
        agg = ev.aggregate_samples([
            {"verdict": "equivalent", "score": 4, "reason": ""},
            {"verdict": "worse", "score": 2, "reason": ""},
            {"verdict": "worse", "score": 2, "reason": ""},
        ])
        self.assertEqual(agg["verdict_mode"], "worse")


# --------------------------------------------------------------------------- #
# Cache hit/miss
# --------------------------------------------------------------------------- #
class CacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cache_dir = self.root / ".cache"

    def _judge(self, return_value):
        calls = []
        def fn(prompt: str) -> Dict[str, Any]:
            calls.append(prompt)
            return return_value
        return fn, calls

    def test_first_call_misses_runs_n_samples(self):
        fn, calls = self._judge(
            {"verdict": "equivalent", "score": 4, "reason": "ok"})
        case = {"id": "c1", "input": {"action": {"kind": "REVIEW"}},
                "expected": {"rec_manager": "archive", "rec_session": "none"}}
        agg = ev.score_case(case=case, actual=_good_actual(),
                            guidelines_hash_s="gh1",
                            judge_name="fake", judge_fn=fn,
                            cache_dir=self.cache_dir, n=3)
        self.assertEqual(len(calls), 3)
        self.assertFalse(agg["cached"])
        self.assertEqual(agg["n"], 3)

    def test_second_call_hits_cache_no_new_judge_calls(self):
        fn, calls = self._judge(
            {"verdict": "equivalent", "score": 4, "reason": "ok"})
        case = {"id": "c1", "input": {"action": {"kind": "REVIEW"}},
                "expected": {"rec_manager": "archive", "rec_session": "none"}}
        ev.score_case(case=case, actual=_good_actual(),
                      guidelines_hash_s="gh1", judge_name="fake",
                      judge_fn=fn, cache_dir=self.cache_dir, n=3)
        calls.clear()
        agg2 = ev.score_case(case=case, actual=_good_actual(),
                             guidelines_hash_s="gh1", judge_name="fake",
                             judge_fn=fn, cache_dir=self.cache_dir, n=3)
        self.assertEqual(calls, [])         # cache hit: no judge spawned
        self.assertTrue(agg2["cached"])
        self.assertEqual(agg2["mean"], 4.0)

    def test_cache_partitioned_per_judge_name(self):
        # Two different judges must not share a cache entry.
        fn_a, calls_a = self._judge(
            {"verdict": "equivalent", "score": 4, "reason": "a"})
        fn_b, calls_b = self._judge(
            {"verdict": "worse", "score": 2, "reason": "b"})
        case = {"id": "c1", "input": {"action": {"kind": "REVIEW"}},
                "expected": {"rec_manager": "archive", "rec_session": "none"}}
        ev.score_case(case=case, actual=_good_actual(),
                      guidelines_hash_s="gh1", judge_name="claude",
                      judge_fn=fn_a, cache_dir=self.cache_dir, n=2)
        ev.score_case(case=case, actual=_good_actual(),
                      guidelines_hash_s="gh1", judge_name="codex",
                      judge_fn=fn_b, cache_dir=self.cache_dir, n=2)
        # Both judges ran their full n=2 even though guidelines+actual
        # match -- the cache namespaces by judge.
        self.assertEqual(len(calls_a), 2)
        self.assertEqual(len(calls_b), 2)

    def test_cache_busts_on_different_actual(self):
        fn, calls = self._judge(
            {"verdict": "equivalent", "score": 4, "reason": "ok"})
        case = {"id": "c1", "input": {"action": {"kind": "REVIEW"}},
                "expected": {"rec_manager": "archive", "rec_session": "none"}}
        ev.score_case(case=case, actual=_good_actual("archive"),
                      guidelines_hash_s="gh1", judge_name="fake",
                      judge_fn=fn, cache_dir=self.cache_dir, n=2)
        calls.clear()
        # Changing the rec text bumps the actual_hash, so the runner must
        # re-judge instead of returning the stale aggregate.
        ev.score_case(case=case, actual=_good_actual("nudge"),
                      guidelines_hash_s="gh1", judge_name="fake",
                      judge_fn=fn, cache_dir=self.cache_dir, n=2)
        self.assertEqual(len(calls), 2)

    def test_cache_busts_on_different_guidelines_hash(self):
        # A guidelines edit -- which is the whole reason to re-run the
        # suite -- must invalidate the cache.
        fn, calls = self._judge(
            {"verdict": "equivalent", "score": 4, "reason": "ok"})
        case = {"id": "c1", "input": {"action": {"kind": "REVIEW"}},
                "expected": {"rec_manager": "archive", "rec_session": "none"}}
        ev.score_case(case=case, actual=_good_actual(),
                      guidelines_hash_s="gh1", judge_name="fake",
                      judge_fn=fn, cache_dir=self.cache_dir, n=2)
        calls.clear()
        ev.score_case(case=case, actual=_good_actual(),
                      guidelines_hash_s="gh2", judge_name="fake",
                      judge_fn=fn, cache_dir=self.cache_dir, n=2)
        self.assertEqual(len(calls), 2)


# --------------------------------------------------------------------------- #
# Per-sample error handling
# --------------------------------------------------------------------------- #
class ScoreCaseErrorHandlingTest(unittest.TestCase):
    def test_one_raising_sample_doesnt_abort_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            calls = {"n": 0}
            def fn(prompt: str) -> Dict[str, Any]:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("simulated judge crash")
                return {"verdict": "equivalent", "score": 4, "reason": "ok"}
            case = {"id": "c1",
                    "input": {"action": {"kind": "REVIEW"}},
                    "expected": {"rec_manager": "archive",
                                 "rec_session": "none"}}
            agg = ev.score_case(case=case, actual=_good_actual(),
                                guidelines_hash_s="gh1", judge_name="fake",
                                judge_fn=fn, cache_dir=cache, n=3)
            # Still 3 samples (one captured as a score=1 raised-error sample).
            self.assertEqual(agg["n"], 3)
            self.assertEqual(len(agg["samples"]), 3)
            raised = [s for s in agg["samples"]
                      if s.get("error") == "raised"]
            self.assertEqual(len(raised), 1)


# --------------------------------------------------------------------------- #
# Suite-level: case loading, runner, summary
# --------------------------------------------------------------------------- #
class RunEvalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cases_dir = self.root / "cases"
        self.out_root = self.root / "out"
        self.guidelines = self.root / "guides.md"
        self.guidelines.write_text("v1 policy")
        _seed_case(self.cases_dir, sid="cse_a",
                   expected_manager="archive", expected_session="none")
        _seed_case(self.cases_dir, sid="cse_b",
                   expected_manager="none",
                   expected_session="answer with Yes")

    def _fake_judges_factory(self, return_value):
        def factory(**_kw):
            def fn(prompt: str) -> Dict[str, Any]:
                return dict(return_value)
            return {"claude": fn, "codex": fn}
        return factory

    def _analyzer_returning(self, actual):
        def analyzer(case):
            return dict(actual)
        return analyzer

    def test_loads_all_cases_writes_per_case_files(self):
        cfg = _cfg()
        summary = ev.run_eval(
            cases_dir=self.cases_dir, out_root=self.out_root,
            guidelines_path=self.guidelines,
            judge_names=["claude"], cfg=cfg,
            judges_factory=self._fake_judges_factory(
                {"verdict": "equivalent", "score": 4, "reason": "ok"}),
            analyzer=self._analyzer_returning(_good_actual()),
            n=3, workers=1)
        self.assertEqual(summary["n_cases"], 2)
        # Output dir is content-addressed by the guidelines hash.
        out_dir = Path(summary["out_dir"])
        self.assertTrue(out_dir.is_dir())
        files = sorted(p.name for p in out_dir.glob("*.json"))
        # Per-case files + the summary.
        self.assertIn("summary.json", files)
        per_case = [f for f in files if f != "summary.json"]
        self.assertEqual(len(per_case), 2)

    def test_per_case_output_carries_judges_block(self):
        cfg = _cfg()
        summary = ev.run_eval(
            cases_dir=self.cases_dir, out_root=self.out_root,
            guidelines_path=self.guidelines,
            judge_names=["claude", "codex"], cfg=cfg,
            judges_factory=self._fake_judges_factory(
                {"verdict": "equivalent", "score": 4, "reason": "ok"}),
            analyzer=self._analyzer_returning(_good_actual()),
            n=3, workers=1)
        out_dir = Path(summary["out_dir"])
        # Read any per-case file and confirm both judges show up.
        case_files = [p for p in out_dir.glob("*.json")
                      if p.name != "summary.json"]
        self.assertTrue(case_files)
        data = json.loads(case_files[0].read_text())
        self.assertIn("claude", data["judges"])
        self.assertIn("codex", data["judges"])
        self.assertEqual(data["judges"]["claude"]["n"], 3)
        self.assertEqual(data["judges"]["codex"]["n"], 3)
        self.assertEqual(data["guidelines_hash"], summary["guidelines_hash"])

    def test_summary_rolls_up_overall_per_judge(self):
        cfg = _cfg()
        summary = ev.run_eval(
            cases_dir=self.cases_dir, out_root=self.out_root,
            guidelines_path=self.guidelines,
            judge_names=["claude"], cfg=cfg,
            judges_factory=self._fake_judges_factory(
                {"verdict": "equivalent", "score": 4, "reason": "ok"}),
            analyzer=self._analyzer_returning(_good_actual()),
            n=3, workers=1)
        overall = summary["overall"]["claude"]
        self.assertEqual(overall["cases"], 2)
        self.assertEqual(overall["mean_score"], 4.0)
        self.assertEqual(overall["verdict_counts"], {"equivalent": 2})

    def test_empty_cases_dir_runs_clean(self):
        empty = self.root / "empty-cases"
        empty.mkdir()
        cfg = _cfg()
        summary = ev.run_eval(
            cases_dir=empty, out_root=self.out_root,
            guidelines_path=self.guidelines,
            judge_names=["claude"], cfg=cfg,
            judges_factory=self._fake_judges_factory(
                {"verdict": "equivalent", "score": 4, "reason": "ok"}),
            analyzer=self._analyzer_returning(_good_actual()),
            n=1, workers=1)
        self.assertEqual(summary["n_cases"], 0)
        self.assertEqual(summary["overall"]["claude"]["cases"], 0)

    def test_thread_pool_runs_all_cases(self):
        # Workers > 1 must still process every case and produce a summary.
        cfg = _cfg()
        # Add a few more cases so the pool actually parallelizes.
        for sid in ("cse_c", "cse_d", "cse_e"):
            _seed_case(self.cases_dir, sid=sid,
                       expected_manager="archive", expected_session="none")
        summary = ev.run_eval(
            cases_dir=self.cases_dir, out_root=self.out_root,
            guidelines_path=self.guidelines,
            judge_names=["claude"], cfg=cfg,
            judges_factory=self._fake_judges_factory(
                {"verdict": "equivalent", "score": 5, "reason": "ok"}),
            analyzer=self._analyzer_returning(_good_actual()),
            n=2, workers=4)
        self.assertEqual(summary["n_cases"], 5)
        self.assertEqual(summary["overall"]["claude"]["cases"], 5)

    def test_unknown_judge_name_raises(self):
        cfg = _cfg()
        with self.assertRaises(ValueError):
            ev.run_eval(
                cases_dir=self.cases_dir, out_root=self.out_root,
                guidelines_path=self.guidelines,
                judge_names=["whoknows"], cfg=cfg,
                judges_factory=self._fake_judges_factory(
                    {"verdict": "equivalent", "score": 4, "reason": "ok"}),
                analyzer=self._analyzer_returning(_good_actual()),
                n=1, workers=1)


# --------------------------------------------------------------------------- #
# Subprocess judge wrappers (argv shape + parse roundtrip)
# --------------------------------------------------------------------------- #
class JudgeSubprocessTest(unittest.TestCase):
    def _runner(self, stdout: str, rc: int = 0):
        calls: List[List[str]] = []
        def fake(cmd, **kw):
            calls.append(list(cmd))
            return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
        return fake, calls

    def test_judge_claude_builds_expected_argv(self):
        runner, calls = self._runner(
            '{"verdict":"equivalent","score":4,"reason":"ok"}')
        out = ev.judge_claude("PROMPT", claude_bin="/bin/claude",
                              model="m1", timeout=60, runner=runner,
                              log=lambda *_: None)
        self.assertEqual(out["verdict"], "equivalent")
        self.assertEqual(calls[0][:5],
                         ["/bin/claude", "-p", "--permission-mode", "plan",
                          "--model"])
        self.assertEqual(calls[0][5], "m1")
        self.assertEqual(calls[0][-1], "PROMPT")

    def test_judge_codex_builds_expected_argv(self):
        runner, calls = self._runner(
            '{"verdict":"worse","score":2,"reason":"x"}')
        out = ev.judge_codex("PROMPT", codex_bin="/bin/codex",
                             timeout=60, runner=runner,
                             log=lambda *_: None)
        self.assertEqual(out["verdict"], "worse")
        self.assertEqual(calls[0], ["/bin/codex", "exec", "PROMPT"])

    def test_subprocess_nonzero_rc_yields_empty_then_parse_error(self):
        runner, _ = self._runner("ignored", rc=2)
        out = ev.judge_claude("p", claude_bin="/bin/claude", timeout=10,
                              runner=runner, log=lambda *_: None)
        self.assertEqual(out["verdict"], "worse")
        self.assertEqual(out["error"], "empty")

    def test_timeout_yields_empty_sample(self):
        def runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))
        out = ev.judge_codex("p", codex_bin="/bin/codex", timeout=5,
                             runner=runner, log=lambda *_: None)
        self.assertEqual(out["verdict"], "worse")
        self.assertEqual(out["error"], "empty")


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #
class CliTest(unittest.TestCase):
    def test_help_returns_zero(self):
        self.assertEqual(ev.main(["--help"]), 0)
        self.assertEqual(ev.main(["help"]), 0)

    def test_no_argv_returns_2(self):
        self.assertEqual(ev.main([]), 2)

    def test_unknown_subcommand_returns_2(self):
        self.assertEqual(ev.main(["bogus"]), 2)

    def test_parse_judges_validates(self):
        self.assertEqual(ev._parse_judges("claude"), ["claude"])
        self.assertEqual(sorted(ev._parse_judges("claude,codex")),
                         ["claude", "codex"])
        with self.assertRaises(ValueError):
            ev._parse_judges("claude,gpt5")


if __name__ == "__main__":
    unittest.main()
