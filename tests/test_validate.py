"""Invariant tests for the /validate milestone validator.

These lock the HONESTY guarantees the skill exists to enforce — the same bar as
the #110 validator and the /report classifier it reuses:

  1. VERIFIED-WORKING requires a RESOLVABLE evidence pointer. A goal whose
     pointer does not resolve is NEVER VERIFIED-WORKING.
  2. `expect:` is a LABEL, never an upgrade. A goal declared expect:working with
     no resolvable proof stays at its honest verdict, and the over-claim is
     flagged as a DISCREPANCY.
  3. A missing/unreachable system is UNVERIFIED; a check that RAN but found
     nothing is FAILED. Neither is ever green.
  4. Anti-fabrication: unproven goals render at their honest verdict and are
     never counted/rendered as working.

The validator lives under skills/validate/scripts/ (loaded by path, since that
dir is not an importable package).
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "validate_mod", REPO / "skills" / "validate" / "scripts" / "validate.py")
validate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate)


def verdict(goal: dict) -> dict:
    return validate.verdict_for_goal(goal)


class ResolvablePointerRequiredTest(unittest.TestCase):
    """Invariant 1: VERIFIED-WORKING <=> a pointer the validator resolved."""

    def test_resolvable_file_is_verified_and_cites_pointer(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"real artifact")
            path = f.name
        try:
            v = verdict({"id": "g", "goal": "x", "evidence": [{"file": path}]})
            self.assertEqual(v["verdict"], "verified")
            # the green verdict must carry the resolved pointer as its proof
            self.assertTrue(v["pointers"], "verified goal must cite an evidence pointer")
            self.assertIn(path, v["pointers"][0])
        finally:
            Path(path).unlink()

    def test_unresolvable_file_is_never_verified(self):
        v = verdict({"id": "g", "goal": "x",
                     "evidence": [{"file": "/nope/does/not/exist-12345.artifact"}]})
        self.assertNotEqual(v["verdict"], "verified")
        self.assertEqual(v["verdict"], "unverified")
        self.assertEqual(v["pointers"], [])

    def test_pending_caps_below_verified_even_with_a_resolved_pointer(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"partial proof")
            path = f.name
        try:
            v = verdict({"id": "g", "goal": "x", "evidence": [{"file": path}],
                         "pending": ["the rest is not proven"]})
            # a resolved pointer + an unproven pending aspect can only be PARTIAL
            self.assertEqual(v["verdict"], "partial")
        finally:
            Path(path).unlink()


class ExpectIsLabelNotUpgradeTest(unittest.TestCase):
    """Invariant 2: `expect:` never upgrades; over-claims are flagged."""

    def test_expect_working_without_proof_stays_honest_and_flags_discrepancy(self):
        v = verdict({"id": "g", "goal": "x", "expect": "working",
                     "evidence": [{"file": "/nope/missing-xyz.json"}]})
        self.assertNotEqual(v["verdict"], "verified")
        self.assertEqual(v["verdict"], "unverified")
        self.assertIsNotNone(v["discrepancy"])
        self.assertIn("UNVERIFIED", v["discrepancy"])

    def test_expect_working_with_no_evidence_is_unverified_not_verified(self):
        v = verdict({"id": "g", "goal": "x", "expect": "working"})
        self.assertEqual(v["verdict"], "unverified")
        self.assertIsNotNone(v["discrepancy"])

    def test_expect_not_yet_is_honored_as_a_label_no_discrepancy(self):
        # declaring a NON-working status is a legitimate label, not an over-claim
        v = verdict({"id": "g", "goal": "x", "expect": "not-yet"})
        self.assertEqual(v["verdict"], "not-yet")
        self.assertIsNone(v["discrepancy"])

    def test_expect_never_downgrades_a_genuinely_verified_goal(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"proof")
            path = f.name
        try:
            v = verdict({"id": "g", "goal": "x", "expect": "not-yet",
                         "evidence": [{"file": path}]})
            # evidence beats the label in BOTH directions: real proof -> verified
            self.assertEqual(v["verdict"], "verified")
            self.assertIsNone(v["discrepancy"])
        finally:
            Path(path).unlink()


class UnreachableAndFailedTest(unittest.TestCase):
    """Invariant 3: missing -> UNVERIFIED; ran-but-empty -> FAILED; never green."""

    def test_all_missing_evidence_is_unverified(self):
        v = verdict({"id": "g", "goal": "x", "evidence": [
            {"file": "~/dev/nowhere/a.json"},
            {"file": "/var/nope/b.json"},
        ]})
        self.assertEqual(v["verdict"], "unverified")
        self.assertNotEqual(v["verdict"], "verified")

    def test_command_that_ran_but_failed_is_failed_not_green(self):
        v = verdict({"id": "g", "goal": "x",
                     "evidence": [{"command": {"run": "false", "expect_rc": 0}}]})
        self.assertEqual(v["verdict"], "failed")

    def test_command_rc_ok_but_expect_match_absent_is_failed(self):
        v = verdict({"id": "g", "goal": "x", "evidence": [
            {"command": {"run": "echo hello", "expect_match": "GOODBYE"}}]})
        self.assertEqual(v["verdict"], "failed")

    def test_grep_pattern_absent_is_failed(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"nothing relevant here")
            path = f.name
        try:
            v = verdict({"id": "g", "goal": "x", "evidence": [
                {"grep": {"pattern": "ABSENT_TOKEN", "path": path}}]})
            self.assertEqual(v["verdict"], "failed")
        finally:
            Path(path).unlink()

    def test_grep_missing_file_is_unverified(self):
        v = verdict({"id": "g", "goal": "x", "evidence": [
            {"grep": {"pattern": "x", "path": "/nope/missing-abc.txt"}}]})
        self.assertEqual(v["verdict"], "unverified")


class AntiFabricationTest(unittest.TestCase):
    """Invariant 4: unproven goals never get upgraded to working in the output."""

    def _milestones(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "proj.yaml").write_text(body)
        return d

    def test_only_resolved_goals_count_as_verified(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"proof")
            real = f.name
        try:
            mdir = self._milestones(f"""
project: proj
goals:
  - id: proven
    goal: this one is real
    evidence:
      - file: {real}
  - id: claimed
    goal: operator claims it works but no proof
    expect: working
    evidence:
      - file: /nope/missing.json
  - id: parked
    goal: not built yet
    expect: not-yet
""")
            res = validate.build_result("proj", mdir)
            by_id = {g["id"]: g for g in res["goals"]}
            self.assertEqual(by_id["proven"]["verdict"], "verified")
            self.assertNotEqual(by_id["claimed"]["verdict"], "verified")
            self.assertNotEqual(by_id["parked"]["verdict"], "verified")

            c = validate.counts(res["goals"])
            self.assertEqual(c["verified"], 1)  # exactly the one with real proof

            # the markdown recap must NOT render the unproven goals as working
            paths = {"html": "h", "narration": "n", "demo": "d"}
            md = validate.markdown_recap(res, paths, "2026-01-01T00:00:00Z")
            self.assertIn("Not proven working here", md)
            self.assertIn("`claimed`", md)
            self.assertIn("Discrepancies", md)  # the over-claim is surfaced
        finally:
            Path(real).unlink()

    def test_worst_first_ordering(self):
        # failed sorts above verified in every table
        goals = [{"verdict": "verified"}, {"verdict": "failed"},
                 {"verdict": "unverified"}, {"verdict": "partial"}]
        ranks = [validate.VERDICTS[g["verdict"]]["rank"] for g in
                 sorted(goals, key=lambda g: validate.VERDICTS[g["verdict"]]["rank"])]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(validate.VERDICTS["failed"]["rank"],
                        validate.VERDICTS["verified"]["rank"])


if __name__ == "__main__":
    unittest.main()
