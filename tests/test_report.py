"""Tests for the /report skill's rigour guards (issue #112).

These cover the four gaps a VISUAL review of a rendered deck exposed, which
exit codes had hidden: unvalidated "tested" badges, in-deck contradictions,
unverifiable health claims, and no freshness signal.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ISO = "%Y-%m-%dT%H:%M:%SZ"


def load_report():
    spec = importlib.util.spec_from_file_location(
        "report_mod", REPO / "skills" / "report" / "scripts" / "report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = load_report()


def ev(**kw):
    kw.setdefault("ts", "2026-07-18T19:05:00Z")
    return kw


def note(text, ts="2026-07-18T19:06:00Z"):
    return {"note": text, "ts": ts}


def testing_for(notes, dirpath=None, stat=None, sha=None):
    rec = {"dir": dirpath, "notes": notes}
    recon = {"current": notes, "superseded": []}
    return rp.gather_testing(rec, recon, {"stat": stat, "sha": sha})


def no_gh(test):
    """Ticket lookups must not hit the network in tests."""
    orig = rp.gather_ticket
    rp.gather_ticket = lambda rec, repo: {
        "number": rec.get("ticket"), "state": None, "title": None,
        "reachable": False}
    test.addCleanup(lambda: setattr(rp, "gather_ticket", orig))


# --------------------------------------------------------------------------- #
# 1. VERIFIED vs SELF-REPORTED testing badges
# --------------------------------------------------------------------------- #
class TestingClassification(unittest.TestCase):

    def test_bare_prose_claim_is_self_reported_not_verified(self):
        """#105's 'tested (5/5)' vouched for tests asserting the wrong
        contract. A prose note with nothing to look at is not verified."""
        t = testing_for([note("tested (5/5)")])
        self.assertEqual(t["state"], "claimed")
        self.assertFalse(t["verified"])
        self.assertEqual(t["pointers"], [])

    def test_resolvable_file_pointer_is_verified(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_thing.py").write_text("def test_x(): pass\n")
            t = testing_for([note("tests pass, see test_thing.py")], dirpath=td)
        self.assertEqual(t["state"], "verified")
        self.assertTrue(any("test_thing.py" in p for p in t["pointers"]))

    def test_nonexistent_path_in_note_does_not_verify(self):
        with tempfile.TemporaryDirectory() as td:
            t = testing_for([note("tested, see tests/test_imaginary.py")], dirpath=td)
        self.assertEqual(t["state"], "claimed")

    def test_test_files_in_landed_commit_verify(self):
        stat = ("abc1234 fix thing\n"
                " remote_control/foo.py |  4 ++--\n"
                " tests/test_foo.py     | 22 ++++++++\n")
        t = testing_for([note("done, tested")], stat=stat, sha="abc1234def")
        self.assertEqual(t["state"], "verified")
        self.assertTrue(any("tests/test_foo.py" in p for p in t["pointers"]))

    def test_commit_touching_no_tests_does_not_verify(self):
        stat = ("abc1234 fix thing\n remote_control/foo.py | 4 ++--\n")
        t = testing_for([note("tested it")], stat=stat, sha="abc1234")
        self.assertEqual(t["state"], "claimed")

    def test_no_mention_of_tests_is_none(self):
        t = testing_for([note("refactored the loader")])
        self.assertEqual(t["state"], "none")
        self.assertFalse(t["has_evidence"])

    def test_deck_never_renders_green_tested_for_a_claim(self):
        """The visual regression itself: no green 'tested' pill for a note."""
        rep = {"manager": "cse_x", "repo": "o/r", "epics": [],
               "freshness": {"level": "fresh", "age_secs": 10,
                             "label": "newest source event 10s old"},
               "workers": [{
                   "worker": "w", "ticket": "105", "brief": "title fix", "ord": 1,
                   "status": "closed", "close_reason": "merged",
                   "latest_note": "tested (5/5)",
                   "reconciled": {"current": [], "superseded": []},
                   "changes": {"sha": None, "stat": None, "files": None,
                               "insertions": None, "deletions": None},
                   "testing": {"state": "claimed", "verified": False,
                               "claimed": True, "has_evidence": True,
                               "pointers": [], "claims": ["tested (5/5)"],
                               "superseded_claims": [],
                               "evidence": ["tested (5/5)"]},
                   "ticket_state": {"number": "105", "state": None,
                                    "title": None, "reachable": False},
               }]}
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        testing = next(s for s in deck["slides"] if s["widget"] == "testing")
        self.assertNotIn('pill ok">✓ VERIFIED', testing["html"])
        self.assertIn("SELF-REPORTED", testing["html"])
        self.assertIn("pill warn", testing["html"])


# --------------------------------------------------------------------------- #
# 2. reconcile to latest state — no in-deck contradiction
# --------------------------------------------------------------------------- #
CONTRADICTION_EVENTS = [
    ev(event="register", worker="w", ticket="107", dir=None, brief="cos-console",
       worker_ord=1, ts="2026-07-19T10:00:00Z"),
    ev(event="update", worker="w", status="reported-done",
       note="holding for OK to commit+push", ts="2026-07-19T11:00:00Z"),
    ev(event="close", worker="w", reason="merged f9b3e6c to origin/main",
       ts="2026-07-19T12:00:00Z"),
]


class Reconciliation(unittest.TestCase):

    def test_superseded_note_is_not_current(self):
        """#107 read 'holding for OK' on one slide, 'merged' on another."""
        rec = rp.fold_workers(CONTRADICTION_EVENTS)["w"]
        recon = rp.reconcile(rec)
        self.assertEqual(recon["status"], "closed")
        cur = " ".join(n["note"] for n in recon["current"])
        old = " ".join(n["note"] for n in recon["superseded"])
        self.assertIn("holding for OK", old)
        self.assertNotIn("holding for OK", cur)
        self.assertIn("merged f9b3e6c", cur)

    def test_latest_note_drives_the_deck(self):
        no_gh(self)
        rep = rp.build_report("cse_x", CONTRADICTION_EVENTS, None, "o/r")
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        whole = " ".join(s["html"] + s["narration"] for s in deck["slides"])
        self.assertNotIn("holding for OK", whole)
        self.assertIn("superseded", whole)

    def test_one_sided_churn_renders_without_none(self):
        """A commit with no deletions must not read '+17677/-None'."""
        rep = {"manager": "m", "repo": "o/r", "epics": [],
               "freshness": {"level": "fresh", "age_secs": 5, "label": "x"},
               "workers": [{
                   "worker": "w", "ticket": "107", "brief": "b", "ord": 1,
                   "status": "closed", "close_reason": "merged f9b3e6c",
                   "latest_note": "merged f9b3e6c",
                   "reconciled": {"current": [], "superseded": []},
                   "changes": {"sha": "f9b3e6c", "stat": "x", "files": 122,
                               "insertions": 17677, "deletions": None},
                   "testing": {"state": "none", "verified": False,
                               "claimed": False, "has_evidence": False,
                               "pointers": [], "claims": [],
                               "superseded_claims": [], "evidence": []},
                   "ticket_state": {"number": "107", "state": None,
                                    "title": None, "reachable": False},
               }]}
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        changes = next(s for s in deck["slides"] if s["widget"] == "changes")
        self.assertNotIn("None", changes["html"])
        self.assertIn("+17677", changes["html"])

    def test_notes_after_the_last_status_change_stay_current(self):
        events = CONTRADICTION_EVENTS[:2] + [
            ev(event="update", worker="w", note="pushed and verified",
               ts="2026-07-19T11:30:00Z")]
        recon = rp.reconcile(rp.fold_workers(events)["w"])
        cur = " ".join(n["note"] for n in recon["current"])
        self.assertIn("holding for OK", cur)
        self.assertIn("pushed and verified", cur)


# --------------------------------------------------------------------------- #
# 3. no unverifiable health claims
# --------------------------------------------------------------------------- #
class HealthClaims(unittest.TestCase):

    def test_narration_never_claims_everything_on_track(self):
        no_gh(self)
        events = [
            ev(event="register", worker="w", ticket="1", dir=None, brief="b",
               worker_ord=1, ts="2026-07-19T10:00:00Z"),
            ev(event="update", worker="w", status="in-progress", note="working",
               ts="2026-07-19T11:00:00Z"),
        ]
        rep = rp.build_report("cse_x", events, None, "o/r")
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        narr = " ".join(s["narration"] for s in deck["slides"]).lower()
        self.assertNotIn("on track", narr)
        self.assertNotIn("everything", narr)

    def test_clean_window_still_scopes_its_claim(self):
        """Even with nothing open, don't assert project health."""
        rep = {"manager": "m", "repo": "o/r", "epics": [],
               "freshness": {"level": "fresh", "age_secs": 5,
                             "label": "newest source event 5s old"},
               "workers": []}
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        oq = next(s for s in deck["slides"] if s["widget"] == "open_questions")
        self.assertNotIn("on track", oq["narration"].lower())
        self.assertIn("health", oq["narration"].lower())

    def test_self_reported_testing_becomes_an_open_question(self):
        rep = {"manager": "m", "repo": "o/r", "epics": [],
               "freshness": {"level": "fresh", "age_secs": 5, "label": "x"},
               "workers": [{
                   "worker": "w", "ticket": "105", "brief": "b", "ord": 1,
                   "status": "closed", "close_reason": "merged",
                   "latest_note": "tested (5/5)",
                   "reconciled": {"current": [], "superseded": []},
                   "changes": {"sha": None, "stat": None, "files": None,
                               "insertions": None, "deletions": None},
                   "testing": {"state": "claimed", "verified": False,
                               "claimed": True, "has_evidence": True,
                               "pointers": [], "claims": ["tested (5/5)"],
                               "superseded_claims": [],
                               "evidence": ["tested (5/5)"]},
                   "ticket_state": {"number": "105", "state": None,
                                    "title": None, "reachable": False},
               }]}
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        oq = next(s for s in deck["slides"] if s["widget"] == "open_questions")
        self.assertIn("SELF-REPORTED", oq["html"])


# --------------------------------------------------------------------------- #
# 4. freshness / staleness indicator
# --------------------------------------------------------------------------- #
class Freshness(unittest.TestCase):

    def test_freshness_levels(self):
        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        for age_hours, level in ((0.5, "fresh"), (30, "aging"), (96, "stale")):
            with self.subTest(age_hours=age_hours):
                events = [ev(event="update", worker="w",
                             ts=(now - timedelta(hours=age_hours)).strftime(ISO))]
                self.assertEqual(rp.freshness(events, now=now)["level"], level)

    def test_no_timestamps_is_unknown_not_fresh(self):
        fr = rp.freshness([{"event": "update", "worker": "w", "ts": ""}])
        self.assertEqual(fr["level"], "unknown")

    def test_stale_deck_carries_a_visible_warning(self):
        fr = {"level": "stale", "age_secs": 4 * 86400,
              "label": "newest source event 4d old"}
        banner = rp.freshness_banner(fr, "2026-07-20T00:00:00Z")
        self.assertIn("STALE", banner)
        self.assertIn("regenerate", banner)
        self.assertIn("2026-07-20T00:00:00Z", banner)

    def test_every_deck_shows_generated_at(self):
        rep = {"manager": "m", "repo": "o/r", "epics": [],
               "freshness": {"level": "fresh", "age_secs": 5,
                             "label": "newest source event 5s old"},
               "workers": []}
        deck = rp.build_deck(rep, "2026-07-20T12:00:00Z", "full history")
        summary = next(s for s in deck["slides"] if s["widget"] == "summary")
        self.assertIn("2026-07-20T12:00:00Z", summary["html"])
        self.assertIn("FRESH", summary["html"])

    def test_stale_data_becomes_an_open_question(self):
        rep = {"manager": "m", "repo": "o/r", "epics": [],
               "freshness": {"level": "stale", "age_secs": 4 * 86400,
                             "label": "newest source event 4d old"},
               "workers": []}
        deck = rp.build_deck(rep, "2026-07-20T00:00:00Z", "full history")
        oq = next(s for s in deck["slides"] if s["widget"] == "open_questions")
        self.assertIn("regenerate", oq["html"])

    def test_human_age(self):
        self.assertEqual(rp.human_age(30), "30s")
        self.assertEqual(rp.human_age(600), "10m")
        self.assertEqual(rp.human_age(7200), "2h")
        self.assertEqual(rp.human_age(4 * 86400), "4d")


if __name__ == "__main__":
    unittest.main()
