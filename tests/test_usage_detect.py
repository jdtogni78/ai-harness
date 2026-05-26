import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from remote_control.config import UsageLimitConfig
from remote_control.usage_limit import monitor
from remote_control.usage_limit.detect import (
    limit_pause_detail,
    parse_reset_epoch,
    pick_resume_target,
    resume_event_body,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "sessions_fixture.json").read_text())["data"]


class DetectionFilterTest(unittest.TestCase):
    def setUp(self):
        self.flagged = {s["id"] for s in FIXTURE if limit_pause_detail(s) is not None}

    def test_flags_bridge_usage_pause(self):
        self.assertIn("cse_bridge_usage", self.flagged)

    def test_flags_cloud_session_pause(self):
        self.assertIn("cse_cloud_session", self.flagged)

    def test_ignores_non_limit_failure(self):
        self.assertNotIn("cse_path_error", self.flagged)

    def test_ignores_running_session_even_with_limit_text(self):
        self.assertNotIn("cse_running_but_limit_text", self.flagged)

    def test_ignores_archived(self):
        self.assertNotIn("cse_archived_limit", self.flagged)

    def test_ignores_healthy_idle(self):
        self.assertNotIn("cse_healthy", self.flagged)

    def test_flags_exactly_the_two_real_pauses(self):
        self.assertEqual(self.flagged, {"cse_bridge_usage", "cse_cloud_session"})


class DetectTrackingTest(unittest.TestCase):
    def test_tracks_pauses_and_clears_recovered(self):
        cfg = UsageLimitConfig.from_env({"HOME": "/Users/x"})
        logs = []
        orig = monitor.list_sessions
        monitor.list_sessions = lambda c, t, l: FIXTURE  # stub network
        try:
            state = {"sessions": {"cse_gone": {"cse_id": "cse_gone",
                                               "first_seen": monitor.now_iso()}}}
            monitor.detect(state, "tok", cfg, logs.append, now=1000.0)
        finally:
            monitor.list_sessions = orig
        self.assertEqual(set(state["sessions"]), {"cse_bridge_usage", "cse_cloud_session"})
        self.assertNotIn("cse_gone", state["sessions"])

    def test_honors_skip_session_ids(self):
        cfg = UsageLimitConfig.from_env({"HOME": "/Users/x",
                                         "USAGE_LIMIT_SKIP_SIDS": "cse_bridge_usage"})
        orig = monitor.list_sessions
        monitor.list_sessions = lambda c, t, l: FIXTURE
        try:
            state = {"sessions": {}}
            monitor.detect(state, "tok", cfg, lambda m: None, now=1000.0)
        finally:
            monitor.list_sessions = orig
        self.assertEqual(set(state["sessions"]), {"cse_cloud_session"})


class ResetParseTest(unittest.TestCase):
    def test_parses_to_same_day_future(self):
        now = datetime(2026, 5, 21, 18, 0, tzinfo=timezone.utc)  # 6pm UTC
        epoch = parse_reset_epoch("session limit · resets 7:50pm UTC", now=now)
        self.assertIsNotNone(epoch)
        dt = datetime.fromtimestamp(epoch, timezone.utc)
        self.assertEqual((dt.hour, dt.minute, dt.day), (19, 50, 21))

    def test_rolls_to_next_day_when_past(self):
        now = datetime(2026, 5, 21, 20, 0, tzinfo=timezone.utc)  # 8pm, after 7:50pm
        epoch = parse_reset_epoch("resets 7:50pm UTC", now=now)
        self.assertEqual(datetime.fromtimestamp(epoch, timezone.utc).day, 22)

    def test_monthly_limit_has_no_reset(self):
        self.assertIsNone(parse_reset_epoch("hit your org's monthly usage limit"))


class ResumeBodyTest(unittest.TestCase):
    def test_wrapped_user_turn_shape(self):
        ev = resume_event_body("continue")["events"][0]
        self.assertEqual(ev["event_type"], "user")
        self.assertEqual(ev["source"], "client")
        self.assertEqual(ev["payload"]["type"], "user")
        self.assertEqual(ev["payload"]["message"]["content"][0]["text"], "continue")


class PickTargetTest(unittest.TestCase):
    def test_oldest_eligible_first(self):
        sessions = {
            "a": {"cse_id": "a", "first_seen": "2026-05-21T10:00:00+00:00",
                  "next_attempt_at": 0, "attempts": 0},
            "b": {"cse_id": "b", "first_seen": "2026-05-21T09:00:00+00:00",
                  "next_attempt_at": 0, "attempts": 0},
        }
        self.assertEqual(pick_resume_target(sessions, now=100, max_attempts=0)["cse_id"], "b")

    def test_respects_backoff(self):
        sessions = {"a": {"cse_id": "a", "first_seen": "x",
                          "next_attempt_at": 1000, "attempts": 0}}
        self.assertIsNone(pick_resume_target(sessions, now=100, max_attempts=0))

    def test_respects_max_attempts_and_unlimited(self):
        sessions = {"a": {"cse_id": "a", "first_seen": "x",
                          "next_attempt_at": 0, "attempts": 3}}
        self.assertIsNone(pick_resume_target(sessions, now=100, max_attempts=3))
        self.assertIsNotNone(pick_resume_target(sessions, now=100, max_attempts=0))


if __name__ == "__main__":
    unittest.main()
