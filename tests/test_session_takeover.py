"""Unit tests for :mod:`remote_control.session_takeover`.

Coverage:
  * is_trivial_turn / classify -- the archive-only vs relaunch split.
  * is_takeover_candidate -- stale-by-time OR disconnected, independently
    (the gap `sessions --stale --disconnected` misses).
  * leading_nick / leading_brackets / retitled_with_nick -- title splicing.
  * main() dry-run and live paths, with spawn/archive/retitle injected so
    nothing hits the network.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from remote_control import session_takeover as takeover


class TrivialTurnTest(unittest.TestCase):
    def test_empty_is_trivial(self):
        self.assertTrue(takeover.is_trivial_turn(""))

    def test_short_greeting_is_trivial(self):
        self.assertTrue(takeover.is_trivial_turn("hi"))
        self.assertTrue(takeover.is_trivial_turn("here"))

    def test_ten_words_is_trivial_eleven_is_not(self):
        ten = " ".join(["word"] * 10)
        eleven = " ".join(["word"] * 11)
        self.assertTrue(takeover.is_trivial_turn(ten))
        self.assertFalse(takeover.is_trivial_turn(eleven))

    def test_real_brief_is_not_trivial(self):
        text = ("please fix the divorce medication tracker so it correctly "
                "shows the dosage schedule for next week")
        self.assertFalse(takeover.is_trivial_turn(text))


class ClassifyTest(unittest.TestCase):
    def test_no_turn_is_archive_only(self):
        decision, reason = takeover.classify("")
        self.assertEqual(decision, takeover.ARCHIVE_ONLY)
        self.assertIn("no recoverable", reason)

    def test_trivial_turn_is_archive_only(self):
        decision, reason = takeover.classify("hi")
        self.assertEqual(decision, takeover.ARCHIVE_ONLY)

    def test_real_work_is_relaunch(self):
        text = "continue implementing the checkout flow refactor we discussed yesterday in detail"
        decision, reason = takeover.classify(text)
        self.assertEqual(decision, takeover.RELAUNCH)


class IsTakeoverCandidateTest(unittest.TestCase):
    def test_running_session_never_a_candidate(self):
        s = {"worker_status": "running", "connection_status": "disconnected"}
        self.assertFalse(takeover.is_takeover_candidate(s, now=1000, older_than_secs=3600))

    def test_disconnected_but_recently_active_is_candidate(self):
        # last_event_at inside the staleness window, but disconnected --
        # the case `sessions --stale --disconnected` misses since it
        # requires BOTH stale-by-time and disconnected.
        s = {"worker_status": "idle", "connection_status": "disconnected",
             "last_event_at": "2026-07-03T00:00:00Z"}
        now = 1782000000.0  # well within 1h of the timestamp above in this fake clock
        self.assertTrue(takeover.is_takeover_candidate(s, now=now, older_than_secs=3600))

    def test_stale_by_time_and_connected_is_candidate(self):
        s = {"worker_status": "idle", "connection_status": "connected",
             "last_event_at": "2020-01-01T00:00:00Z"}
        import time
        self.assertTrue(takeover.is_takeover_candidate(s, now=time.time(), older_than_secs=3600))

    def test_idle_recent_and_connected_is_not_a_candidate(self):
        import time
        from datetime import datetime, timezone
        now = time.time()
        recent = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
        s = {"worker_status": "idle", "connection_status": "connected",
             "last_event_at": recent}
        self.assertFalse(takeover.is_takeover_candidate(s, now=now, older_than_secs=3600))


class TitleSplicingTest(unittest.TestCase):
    def test_leading_nick_present(self):
        self.assertEqual(takeover.leading_nick("[DEV.m5][sub] body"), "DEV.m5")

    def test_leading_nick_absent(self):
        self.assertIsNone(takeover.leading_nick("no brackets here"))

    def test_leading_brackets_multi(self):
        self.assertEqual(takeover.leading_brackets("[A][B] body"), ["A", "B"])

    def test_leading_brackets_none(self):
        self.assertEqual(takeover.leading_brackets("body only"), [])

    def test_retitled_with_nick_preserves_subname(self):
        got = takeover.retitled_with_nick("[relaunch-abc] auto-spawned", "DEV.m5")
        self.assertEqual(got, "[DEV.m5][relaunch-abc] auto-spawned")

    def test_retitled_with_nick_no_existing_bracket(self):
        got = takeover.retitled_with_nick("auto-spawned", "DEV.m5")
        self.assertEqual(got, "[DEV.m5] auto-spawned")


class ExtractRelaunchedCseTest(unittest.TestCase):
    def test_extracts_new_cse_from_relaunch_output(self):
        out = "relaunch: cse_OLD -> cse_NEW (record=/tmp/x.json)\n"
        self.assertEqual(takeover._extract_relaunched_cse(out), "cse_NEW")

    def test_no_match_returns_none(self):
        self.assertIsNone(takeover._extract_relaunched_cse("nothing useful\n"))


class MainDryRunTest(unittest.TestCase):
    def test_dry_run_classifies_without_acting(self):
        sessions = [
            {"id": "cse_A", "title": "[AO] real work", "worker_status": "idle",
             "connection_status": "disconnected", "last_event_at": "2026-07-03T00:00:00Z"},
            {"id": "cse_B", "title": "[AO] hi", "worker_status": "idle",
             "connection_status": "disconnected", "last_event_at": "2026-07-03T00:00:00Z"},
        ]
        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value="tok"), \
             mock.patch("remote_control.usage_limit.monitor.list_sessions", return_value=sessions), \
             mock.patch.object(takeover, "last_user_turn",
                               side_effect=lambda cse_id, **kw:
                               "please continue the long-running migration work we discussed yesterday in detail" if cse_id == "cse_A" else "hi"), \
             mock.patch("remote_control.usage_limit.monitor.archive_session") as archive_mock, \
             mock.patch("remote_control.relaunch.main") as relaunch_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = takeover.main(["--dry-run"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("cse_A", out)
        self.assertIn("relaunch (real work in progress)", out)
        self.assertIn("cse_B", out)
        self.assertIn("archive-only", out)
        archive_mock.assert_not_called()
        relaunch_mock.assert_not_called()

    def test_no_candidates_prints_zero(self):
        sessions = [{"id": "cse_A", "title": "x", "worker_status": "running",
                     "connection_status": "connected", "last_event_at": "2026-07-03T00:00:00Z"}]
        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value="tok"), \
             mock.patch("remote_control.usage_limit.monitor.list_sessions", return_value=sessions):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = takeover.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("0 takeover candidate(s)", buf.getvalue())


class MainLiveArchiveOnlyTest(unittest.TestCase):
    def test_archive_only_candidate_gets_archived(self):
        sessions = [
            {"id": "cse_B", "title": "[AO] hi", "worker_status": "idle",
             "connection_status": "disconnected", "last_event_at": "2026-07-03T00:00:00Z"},
        ]
        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value="tok"), \
             mock.patch("remote_control.usage_limit.monitor.list_sessions", return_value=sessions), \
             mock.patch.object(takeover, "last_user_turn", return_value="hi"), \
             mock.patch("remote_control.usage_limit.monitor.archive_session",
                        return_value=(200, {})) as archive_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = takeover.main([])
        self.assertEqual(rc, 0)
        archive_mock.assert_called_once()
        self.assertEqual(archive_mock.call_args[0][2], "cse_B")
        self.assertIn("relaunched: 0  archived: 1  failed: 0", buf.getvalue())


class MainLiveRelaunchTest(unittest.TestCase):
    def test_relaunch_candidate_spawns_retitles_and_archives_original(self):
        sessions = [
            {"id": "cse_OLD", "title": "[DEV.m5] divorce medication work",
             "worker_status": "idle", "connection_status": "disconnected",
             "last_event_at": "2026-07-03T00:00:00Z"},
        ]

        def fake_relaunch_main(argv):
            print("relaunch: cse_OLD -> cse_NEW (record=/tmp/rec.json)")
            return 0

        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value="tok"), \
             mock.patch("remote_control.usage_limit.monitor.list_sessions", return_value=sessions), \
             mock.patch.object(takeover, "last_user_turn",
                               return_value="please continue the long-running migration work we discussed together again yesterday"), \
             mock.patch("remote_control.relaunch.main", side_effect=fake_relaunch_main), \
             mock.patch("remote_control.usage_limit.monitor.api_request",
                        return_value=(200, {"title": "[relaunch-old] auto-spawned"})), \
             mock.patch("remote_control.session_takeover.set_title",
                        return_value=(200, {})) as set_title_mock, \
             mock.patch("remote_control.usage_limit.monitor.archive_session",
                        return_value=(200, {})) as archive_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = takeover.main([])

        self.assertEqual(rc, 0)
        set_title_mock.assert_called_once()
        applied_title = set_title_mock.call_args[0][3]
        self.assertEqual(applied_title, "[DEV.m5][relaunch-old] auto-spawned")
        archive_mock.assert_called_once()
        self.assertEqual(archive_mock.call_args[0][2], "cse_OLD")
        self.assertIn("relaunched: 1  archived: 0  failed: 0", buf.getvalue())

    def test_failed_relaunch_counts_as_failed_and_skips_archive(self):
        sessions = [
            {"id": "cse_OLD", "title": "[DEV.m5] real work here", "worker_status": "idle",
             "connection_status": "disconnected", "last_event_at": "2026-07-03T00:00:00Z"},
        ]
        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value="tok"), \
             mock.patch("remote_control.usage_limit.monitor.list_sessions", return_value=sessions), \
             mock.patch.object(takeover, "last_user_turn",
                               return_value="please continue the long-running migration work we discussed together again yesterday"), \
             mock.patch("remote_control.relaunch.main", return_value=1), \
             mock.patch("remote_control.usage_limit.monitor.archive_session") as archive_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = takeover.main([])
        self.assertEqual(rc, 1)
        archive_mock.assert_not_called()
        self.assertIn("failed: 1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
