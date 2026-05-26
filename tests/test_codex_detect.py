import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from remote_control.config import CodexConfig
from remote_control.usage_limit import monitor
from remote_control.usage_limit.codex import (
    backoff_until_reset,
    codex_blocked_sessions,
    evaluate_block,
    read_rollout,
)

NOW = 1_000_000.0
P_RESET = NOW + 3600        # 5h window resets in 1h
S_RESET = NOW + 100_000     # weekly window resets much later


def rl(primary_pct, secondary_pct, reached=None):
    return {
        "limit_id": "codex", "plan_type": "plus",
        "primary": {"used_percent": primary_pct, "window_minutes": 300,
                    "resets_at": P_RESET},
        "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                      "resets_at": S_RESET},
        "rate_limit_reached_type": reached,
    }


def write_rollout(path: Path, sid: str, cwd: str, rate_limits):
    lines = [json.dumps({"type": "session_meta",
                         "payload": {"id": sid, "cwd": cwd,
                                     "originator": "test", "cli_version": "x"}})]
    if rate_limits is not None:
        lines.append(json.dumps({"type": "event_msg",
                                 "payload": {"type": "token_count",
                                             "rate_limits": rate_limits}}))
    path.write_text("\n".join(lines) + "\n")


def codex_cfg(env=None):
    base = {"HOME": "/Users/x"}
    base.update(env or {})
    return CodexConfig.from_env(base)


class EvaluateBlockTest(unittest.TestCase):
    def test_healthy_not_blocked(self):
        blocked, reset, detail = evaluate_block(rl(90, 48), threshold=100)
        self.assertFalse(blocked)
        self.assertIsNone(reset)

    def test_primary_exhausted_uses_primary_reset(self):
        blocked, reset, detail = evaluate_block(rl(100, 20), threshold=100)
        self.assertTrue(blocked)
        self.assertEqual(reset, P_RESET)
        self.assertIn("5h", detail)

    def test_weekly_exhausted_uses_weekly_reset(self):
        blocked, reset, _ = evaluate_block(rl(10, 100), threshold=100)
        self.assertTrue(blocked)
        self.assertEqual(reset, S_RESET)

    def test_both_exhausted_waits_for_latest(self):
        blocked, reset, _ = evaluate_block(rl(100, 100), threshold=100)
        self.assertTrue(blocked)
        self.assertEqual(reset, S_RESET)

    def test_reached_flag_blocks_even_under_threshold(self):
        blocked, reset, detail = evaluate_block(rl(40, 30, reached="primary"),
                                                threshold=100)
        self.assertTrue(blocked)
        self.assertEqual(reset, P_RESET)  # reached names primary -> wait on primary
        self.assertIn("reached=primary", detail)

    def test_reached_unknown_name_falls_back_to_shortest_window(self):
        blocked, reset, _ = evaluate_block(rl(40, 30, reached="usage_limit"),
                                           threshold=100)
        self.assertTrue(blocked)
        self.assertEqual(reset, P_RESET)  # shortest known window (5h)

    def test_none_rate_limits(self):
        self.assertEqual(evaluate_block(None, 100), (False, None, ""))

    def test_lower_threshold_trips_earlier(self):
        blocked, _, _ = evaluate_block(rl(90, 10), threshold=85)
        self.assertTrue(blocked)


class BackoffTest(unittest.TestCase):
    def test_waits_for_future_reset(self):
        next_at, _ = backoff_until_reset(P_RESET, attempts=1, now=NOW,
                                         backoffs_secs=(300, 900, 1800))
        self.assertEqual(next_at, P_RESET + 30)

    def test_exponential_when_no_reset(self):
        self.assertEqual(backoff_until_reset(None, 1, NOW, (300, 900, 1800))[0],
                         NOW + 300)
        self.assertEqual(backoff_until_reset(None, 2, NOW, (300, 900, 1800))[0],
                         NOW + 900)
        self.assertEqual(backoff_until_reset(None, 9, NOW, (300, 900, 1800))[0],
                         NOW + 1800)  # capped at last step

    def test_past_reset_falls_through_to_backoff(self):
        next_at, _ = backoff_until_reset(NOW - 10, 1, NOW, (300,))
        self.assertEqual(next_at, NOW + 300)


class ReadRolloutTest(unittest.TestCase):
    def test_extracts_id_cwd_and_last_rate_limits(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rollout-x.jsonl"
            write_rollout(p, "uuid-1", "/repo", rl(100, 5))
            meta = read_rollout(str(p))
            self.assertEqual(meta["id"], "uuid-1")
            self.assertEqual(meta["cwd"], "/repo")
            self.assertEqual(meta["rate_limits"]["primary"]["used_percent"], 100)

    def test_no_session_meta_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rollout-y.jsonl"
            p.write_text(json.dumps({"type": "event_msg", "payload": {}}) + "\n")
            self.assertIsNone(read_rollout(str(p)))


class ScanTest(unittest.TestCase):
    def _dir(self, d):
        sub = Path(d) / "2026" / "05" / "22"
        sub.mkdir(parents=True)
        return sub

    def test_returns_only_blocked_with_cwd_and_title(self):
        with tempfile.TemporaryDirectory() as d:
            sub = self._dir(d)
            write_rollout(sub / "rollout-a.jsonl", "blk", "/repo/a", rl(100, 5))
            write_rollout(sub / "rollout-b.jsonl", "ok", "/repo/b", rl(50, 5))
            out = codex_blocked_sessions(str(Path(d)), NOW, threshold=100,
                                         recent_secs=10_000,
                                         titles={"blk": "Blocked one"})
            self.assertEqual([s["codex_id"] for s in out], ["blk"])
            self.assertEqual(out[0]["cwd"], "/repo/a")
            self.assertEqual(out[0]["title"], "Blocked one")
            self.assertEqual(out[0]["reset_epoch"], P_RESET)

    def test_recent_secs_excludes_stale_rollouts(self):
        with tempfile.TemporaryDirectory() as d:
            sub = self._dir(d)
            p = sub / "rollout-old.jsonl"
            write_rollout(p, "blk", "/repo", rl(100, 5))
            old = NOW - 20_000
            os.utime(p, (old, old))
            out = codex_blocked_sessions(str(Path(d)), NOW, threshold=100,
                                         recent_secs=10_000)
            self.assertEqual(out, [])

    def test_newest_rollout_wins_recovered_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            sub = self._dir(d)
            old = sub / "rollout-1.jsonl"
            new = sub / "rollout-2.jsonl"
            write_rollout(old, "sess", "/repo", rl(100, 5))    # was blocked
            write_rollout(new, "sess", "/repo", rl(20, 5))     # resumed/healthy
            os.utime(old, (NOW - 500, NOW - 500))
            os.utime(new, (NOW - 10, NOW - 10))
            out = codex_blocked_sessions(str(Path(d)), NOW, threshold=100,
                                         recent_secs=10_000)
            self.assertEqual(out, [])


class DetectCodexTest(unittest.TestCase):
    def _sessions(self, d, sid, cwd, rate_limits):
        sub = Path(d) / "sessions" / "2026" / "05" / "22"
        sub.mkdir(parents=True, exist_ok=True)
        write_rollout(sub / f"rollout-{sid}.jsonl", sid, cwd, rate_limits)

    def test_tracks_then_clears_recovered(self):
        with tempfile.TemporaryDirectory() as d:
            self._sessions(d, "s1", "/repo", rl(100, 5))
            cfg = codex_cfg({"CODEX_HOME": d, "REMOTE_CONTROL_LOGDIR": d})
            state = {"sessions": {}}
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertIn("s1", state["sessions"])
            # rollout now shows healthy -> next detect clears it
            self._sessions(d, "s1", "/repo", rl(10, 5))
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertNotIn("s1", state["sessions"])

    def test_honors_skip_ids(self):
        with tempfile.TemporaryDirectory() as d:
            self._sessions(d, "s1", "/repo", rl(100, 5))
            cfg = codex_cfg({"CODEX_HOME": d, "REMOTE_CONTROL_LOGDIR": d,
                             "USAGE_LIMIT_CODEX_SKIP_SIDS": "s1"})
            state = {"sessions": {}}
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertEqual(state["sessions"], {})

    def test_keeps_entry_with_live_resume_pid(self):
        with tempfile.TemporaryDirectory() as d:
            # No blocked rollouts on disk, but an in-flight resume pid -> keep.
            import time as _t
            nowt = _t.time()
            cfg = codex_cfg({"CODEX_HOME": d, "REMOTE_CONTROL_LOGDIR": d})
            state = {"sessions": {"s1": {"codex_id": "s1", "pid": os.getpid(),
                                         "spawned_at": nowt, "first_seen": "x",
                                         "next_attempt_at": 0, "attempts": 1}}}
            monitor.detect_codex(state, cfg, lambda m: None, now=nowt)
            self.assertIn("s1", state["sessions"])

    def test_keeps_quiet_session_waiting_on_long_cap(self):
        # A weekly cap: the rollout goes quiet (no further turns) and falls out
        # of the conservative recent_secs scan, but it is still rate-limited and
        # must be kept so we resume once the window resets.
        with tempfile.TemporaryDirectory() as d:
            self._sessions(d, "s1", "/repo", rl(10, 100))  # weekly window exhausted
            roll = Path(d) / "sessions" / "2026" / "05" / "22" / "rollout-s1.jsonl"
            cfg = codex_cfg({"CODEX_HOME": d, "REMOTE_CONTROL_LOGDIR": d})
            os.utime(roll, (NOW - 60, NOW - 60))           # fresh -> first detect
            state = {"sessions": {}}
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertIn("s1", state["sessions"])
            self.assertEqual(state["sessions"]["s1"]["reset_epoch"], S_RESET)
            # Quiet: older than recent_secs but within gc_age, still blocked.
            quiet = NOW - (cfg.recent_secs + 3600)
            os.utime(roll, (quiet, quiet))
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertIn("s1", state["sessions"])
            self.assertEqual(state["sessions"]["s1"]["reset_epoch"], S_RESET)

    def test_gc_keeps_session_waiting_on_future_reset(self):
        # first_seen older than gc_age but the window still resets in the future
        # -> the resume is still makeable, so it must not be GC'd.
        with tempfile.TemporaryDirectory() as d:
            self._sessions(d, "s1", "/repo", rl(10, 100))
            roll = Path(d) / "sessions" / "2026" / "05" / "22" / "rollout-s1.jsonl"
            os.utime(roll, (NOW - 1000, NOW - 1000))
            cfg = codex_cfg({"CODEX_HOME": d, "REMOTE_CONTROL_LOGDIR": d})
            old = datetime.fromtimestamp(
                NOW - cfg.gc_age_secs - 100, timezone.utc).isoformat()
            state = {"sessions": {"s1": {
                "codex_id": "s1", "cwd": "/repo", "title": None,
                "status_detail": None, "reset_epoch": S_RESET,
                "first_seen": old, "attempts": 1, "last_attempt_at": None,
                "next_attempt_at": 0, "pid": None, "spawned_at": None}}}
            monitor.detect_codex(state, cfg, lambda m: None, now=NOW)
            self.assertIn("s1", state["sessions"])


class ResumeCodexTest(unittest.TestCase):
    def test_dry_run_does_not_spawn(self):
        cfg = codex_cfg({"USAGE_LIMIT_DRY_RUN": "1"})

        def boom(*a, **k):
            raise AssertionError("popen called in dry-run")

        s = {"codex_id": "s1", "cwd": "/tmp", "attempts": 0}
        monitor.attempt_resume_codex(s, cfg, lambda m: None, now=NOW, popen=boom)
        self.assertEqual(s["attempts"], 0)
        self.assertEqual(s["next_attempt_at"], NOW + cfg.resume_interval)

    def test_live_spawns_codex_exec_resume(self):
        calls = {}

        class FakeProc:
            pid = 4242

        def fake_popen(cmd, cwd=None, **k):
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            return FakeProc()

        with tempfile.TemporaryDirectory() as d:
            cfg = codex_cfg({"USAGE_LIMIT_DRY_RUN": "0",
                             "REMOTE_CONTROL_LOGDIR": d,
                             "REMOTE_CONTROL_CODEX_BIN": "/bin/echo"})
            s = {"codex_id": "uuid-9", "cwd": d, "attempts": 0,
                 "reset_epoch": NOW + 3600}
            monitor.attempt_resume_codex(s, cfg, lambda m: None, now=NOW,
                                         popen=fake_popen)
            self.assertEqual(calls["cmd"],
                             ["/bin/echo", "exec", "resume", "uuid-9", "continue"])
            self.assertEqual(calls["cwd"], d)
            self.assertEqual(s["pid"], 4242)
            self.assertEqual(s["attempts"], 1)
            self.assertGreaterEqual(s["next_attempt_at"], NOW + 3600)

    def test_skips_when_cwd_missing(self):
        cfg = codex_cfg({"USAGE_LIMIT_DRY_RUN": "0",
                         "REMOTE_CONTROL_CODEX_BIN": "/bin/echo"})

        def boom(*a, **k):
            raise AssertionError("should not spawn without a valid cwd")

        s = {"codex_id": "s1", "cwd": "/nope/does/not/exist", "attempts": 0}
        monitor.attempt_resume_codex(s, cfg, lambda m: None, now=NOW, popen=boom)
        self.assertEqual(s["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
