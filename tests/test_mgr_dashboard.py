"""Tests for the sessions-by-manager dashboard: title parsing, grouping, and the
cse_ -> transcript-uuid nearest-timestamp mapper.

The tool lives under ``skills/mgr-dashboard/`` (a tool dir, not a package), so we
add it to ``sys.path`` before importing. Pure-logic only — no harness, no API,
no network; the mapper is exercised with synthetic timestamps and a tmp
transcript tree so the whole suite is hermetic. Run with:

    python3 -m unittest tests.test_mgr_dashboard
"""
import sys
import tempfile
import unittest
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1] / "skills" / "mgr-dashboard"
sys.path.insert(0, str(_TOOL))

import mgr_dashboard as md  # noqa: E402


class TitleParseTest(unittest.TestCase):
    def test_manager_first_form(self):
        t = md.parse_title("[MGR-20] skill-manager — skills domain (1 worker)")
        self.assertEqual((t.role, t.manager, t.worker), ("manager", 20, None))

    def test_manager_nick_leading_legacy(self):
        t = md.parse_title("[AH.m5][MGR-12] session-naming / titles system")
        self.assertEqual((t.role, t.manager), ("manager", 12))
        self.assertEqual(t.nick, "AH.m5")

    def test_worker_combined_form_with_ticket(self):
        t = md.parse_title("[DST.m5][MGR17-W9][#71] Investigate duplicate-timestamp")
        self.assertEqual(
            (t.role, t.manager, t.worker, t.ticket), ("worker", 17, 9, 71)
        )

    def test_worker_combined_no_ticket(self):
        t = md.parse_title("[DP.m5][MGR1-W41] Austin sale scenario")
        self.assertEqual((t.role, t.manager, t.worker), ("worker", 1, 41))

    def test_worker_split_manager_first_form(self):
        # future manager-first split form [MGR13][W5]
        t = md.parse_title("[MGR13][W5][#44] spend v2 — WF gap")
        self.assertEqual(
            (t.role, t.manager, t.worker, t.ticket), ("worker", 13, 5, 44)
        )

    def test_worker_linkage_in_body_not_brackets(self):
        t = md.parse_title(
            "[DP.m5] MGR7-W21 — fundeck: bucket/3x $10k illustrative pitch"
        )
        self.assertEqual((t.role, t.manager, t.worker), ("worker", 7, 21))

    def test_nomrg_explicit_unmanaged(self):
        t = md.parse_title("[NOMRG][AH.m5] boss answers feed")
        self.assertEqual(t.role, "unmanaged")
        self.assertEqual(t.nick, "AH.m5")

    def test_inbox_unmanaged(self):
        self.assertEqual(md.parse_title("[AH.m5][INBOX] Boss answers feed").role,
                         "unmanaged")

    def test_nick_only_unmanaged_keeps_ticket(self):
        t = md.parse_title("[AH.m5] #158 sessions-by-manager dashboard")
        self.assertEqual(t.role, "unmanaged")
        self.assertEqual(t.ticket, 158)

    def test_plain_unmanaged(self):
        self.assertEqual(md.parse_title("[Dev.mini] Placeholder session").role,
                         "unmanaged")

    def test_manager_not_confused_with_worker(self):
        # MGR-17 (manager) must not be read as a worker of some manager 17
        self.assertEqual(
            md.parse_title("[DEV.m5][MGR-17] dstrader stuff").role, "manager"
        )


def _sess(sid, title):
    return {"id": sid, "title": title, "title_info": md.parse_title(title)}


class GroupingTest(unittest.TestCase):
    def test_nests_workers_and_orders_unmanaged_last(self):
        sessions = [
            _sess("u", "[AH.m5] loose end"),
            _sess("w2", "[X][MGR5-W2] second"),
            _sess("m5", "[MGR-5] the manager"),
            _sess("w1", "[X][MGR5-W1] first"),
            _sess("m3", "[MGR-3] earlier manager"),
        ]
        groups = md.build_groups(sessions)
        self.assertEqual([g.manager_ordinal for g in groups], [3, 5, None])
        g5 = groups[1]
        self.assertEqual(g5.manager_session["id"], "m5")
        self.assertEqual([w["id"] for w in g5.workers], ["w1", "w2"])
        self.assertEqual(groups[-1].others[0]["id"], "u")

    def test_worker_without_live_manager_still_grouped(self):
        groups = md.build_groups([_sess("w", "[X][MGR9-W1] orphan worker")])
        self.assertEqual(groups[0].manager_ordinal, 9)
        self.assertIsNone(groups[0].manager_session)
        self.assertEqual(groups[0].workers[0]["id"], "w")


class GreedyMatchTest(unittest.TestCase):
    def test_nearest_wins_each_used_once(self):
        m = md.greedy_match(
            {"a": 100.0, "b": 200.0}, {"ua": 101.0, "ub": 199.0, "far": 1e5}
        )
        self.assertEqual(m["a"], ("ua", 1.0))
        self.assertEqual(m["b"], ("ub", 1.0))
        self.assertNotIn("far", [v[0] for v in m.values()])

    def test_respects_tolerance(self):
        self.assertNotIn("a", md.greedy_match({"a": 0.0}, {"u": 1000.0}, tol_s=300.0))

    def test_contested_transcript_goes_to_closest(self):
        m = md.greedy_match(
            {"a": 100.0, "b": 103.0}, {"shared": 102.0, "other": 108.0}
        )
        self.assertEqual(m["b"][0], "shared")  # b (Δ1) beats a (Δ2)
        self.assertEqual(m["a"][0], "other")

    def test_confidence_bands(self):
        self.assertEqual(md.confidence(1.0), "high")
        self.assertEqual(md.confidence(30.0), "medium")
        self.assertEqual(md.confidence(200.0), "low")
        self.assertEqual(md.confidence(9999.0), "none")
        self.assertEqual(md.confidence(None), "none")


class MapSessionsTest(unittest.TestCase):
    def _tree(self, tmp, entries):
        proj = Path(tmp) / "-Users-dev-ai-harness"
        proj.mkdir()
        for uuid, ts in entries:
            (proj / f"{uuid}.jsonl").write_text(f'{{"timestamp":"{ts}"}}\n')
        return Path(tmp)

    def test_index_ignores_non_uuid_and_reads_last_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(
                tmp, [("615790b1-2027-4a9b-9aee-25c54d8e5ec5",
                       "2026-07-31T14:51:16.322Z")]
            )
            # append a later record + a bogus non-uuid file
            proj = root / "-Users-dev-ai-harness"
            with open(proj / "615790b1-2027-4a9b-9aee-25c54d8e5ec5.jsonl", "a") as f:
                f.write('{"timestamp":"2026-07-31T14:51:18.000Z"}\n')
            (proj / "notes.jsonl").write_text('{"timestamp":"2026-07-31T00:00:00Z"}\n')
            txs = md.index_transcripts(root)
            self.assertEqual(len(txs), 1)
            self.assertEqual(txs[0].last_epoch,
                             md._iso_to_epoch("2026-07-31T14:51:18.000Z"))

    def test_end_to_end_repo_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            u_me = "615790b1-2027-4a9b-9aee-25c54d8e5ec5"
            u_mgr = "4282d4e7-5b62-4bb9-91f8-cf29851c7340"
            root = self._tree(
                tmp,
                [(u_me, "2026-07-31T14:51:16.322Z"),
                 (u_mgr, "2026-07-31T14:46:55.737Z")],
            )
            sessions = [
                {"id": "cse_me", "last_event_at": "2026-07-31T14:51:16.459Z",
                 "config": {"sources": [{"url": "https://github.com/o/ai-harness"}]}},
                {"id": "cse_mgr", "last_event_at": "2026-07-31T14:46:57.691Z",
                 "config": {"sources": [{"url": "https://github.com/o/ai-harness"}]}},
            ]
            m = md.map_sessions_to_uuids(sessions, md.index_transcripts(root))
            self.assertEqual(m["cse_me"][0], u_me)
            self.assertEqual(m["cse_mgr"][0], u_mgr)
            self.assertEqual(md.confidence(m["cse_me"][1]), "high")

    def test_drops_low_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(
                tmp, [("615790b1-2027-4a9b-9aee-25c54d8e5ec5",
                       "2020-01-01T00:00:00Z")]
            )
            sessions = [
                {"id": "cse_x", "last_event_at": "2026-07-31T14:51:16.459Z",
                 "config": {"sources": [{"url": "https://github.com/o/ai-harness"}]}}
            ]
            m = md.map_sessions_to_uuids(sessions, md.index_transcripts(root))
            self.assertEqual(m["cse_x"], (None, None))


if __name__ == "__main__":
    unittest.main()
