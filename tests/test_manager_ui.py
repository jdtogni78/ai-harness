"""Tests for the manager-ui review server (remote_control.manager_ui)."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from remote_control.config import ManagerConfig
from remote_control import manager
from remote_control.manager import Action, ANSWER, REVIEW, SKIP
from remote_control import manager_ui as ui


def _cfg(tmp: str) -> ManagerConfig:
    return ManagerConfig.from_env({
        "REMOTE_CONTROL_LOGDIR": os.path.join(tmp, "logs"),
        "REMOTE_CONTROL_DEV": tmp,
        "MANAGER_ALLOWLIST_FILE": "/nonexistent",
        "MANAGER_GUIDELINES_FILE": os.path.join(tmp, "guidelines.md"),
    })


def _proc(stdout="", rc=0, stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class FeedbackGuidelinesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(self._tmp.name)

    def test_feedback_appends_notes(self):
        f = self.cfg.feedback_file
        ui.add_feedback(f, "review:cse_1:", "too aggressive")
        ui.add_feedback(f, "review:cse_1:", "better now")
        notes = ui.load_feedback(f)["review:cse_1:"]
        self.assertEqual([n["text"] for n in notes], ["too aggressive", "better now"])
        self.assertTrue(notes[-1]["at"])

    def test_feedback_migrates_legacy_single_note(self):
        f = self.cfg.feedback_file
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"review:cse_1:": {"feedback": "old note", "at": "t"}}))
        self.assertEqual(ui.load_feedback(f)["review:cse_1:"][0]["text"], "old note")

    def test_feedback_missing_file(self):
        self.assertEqual(ui.load_feedback(self.cfg.feedback_file), {})

    def test_guidelines_roundtrip(self):
        self.assertEqual(ui.load_guidelines(self.cfg), "")            # missing -> ""
        ui.save_guidelines(self.cfg, "NEVER force-push to main")
        self.assertEqual(ui.load_guidelines(self.cfg), "NEVER force-push to main")

    def test_api_feedback_valid_and_invalid(self):
        code, _ = ui.api_feedback(self.cfg, {"sig": "review:cse_1:", "feedback": "ok"})
        self.assertEqual(code, 200)
        self.assertEqual(ui.load_feedback(self.cfg.feedback_file)["review:cse_1:"][0]["text"], "ok")
        self.assertEqual(ui.api_feedback(self.cfg, {"feedback": "x"})[0], 400)
        self.assertEqual(ui.api_feedback(self.cfg, {"sig": "x", "feedback": "  "})[0], 400)

    def test_api_guidelines_get_and_post(self):
        self.assertEqual(ui.api_guidelines_post(self.cfg, {"text": "be careful"})[0], 200)
        code, body = ui.api_guidelines_get(self.cfg)
        self.assertEqual(body["text"], "be careful")
        self.assertEqual(ui.api_guidelines_post(self.cfg, {})[0], 400)

    def test_api_guidelines_suggest(self):
        ui.api_guidelines_post(self.cfg, {"text": "# guidelines"})
        ui.add_feedback(self.cfg.feedback_file, "review:cse_1:", "close idle threads")
        code, body = ui.api_guidelines_suggest(
            self.cfg, runner=lambda *a, **k: _proc(stdout="# revised\nGUIDELINE: close idle"))
        self.assertTrue(body["ok"])
        self.assertIn("revised", body["text"])

    def test_api_guidelines_suggest_no_feedback(self):
        code, body = ui.api_guidelines_suggest(self.cfg, runner=lambda *a, **k: _proc(stdout="x"))
        self.assertFalse(body["ok"])


class SendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(self._tmp.name)

    def test_send_posts_text_to_session(self):
        calls = {}

        def api(ucfg, method, path, token, body=None):
            calls["path"], calls["body"] = path, body
            return 200, {}

        code, body = ui.api_send(self.cfg, {"session_id": "cse_1", "text": "do the thing"},
                                 get_token=lambda *a, **k: "tok", api=api)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("/sessions/cse_1/events", calls["path"])

    def test_send_needs_session_and_text(self):
        self.assertEqual(ui.api_send(self.cfg, {"session_id": "cse_1"})[0], 400)
        self.assertEqual(ui.api_send(self.cfg, {"text": "x"})[0], 400)

    def test_send_no_token(self):
        code, body = ui.api_send(self.cfg, {"session_id": "cse_1", "text": "x"},
                                 get_token=lambda *a, **k: "")
        self.assertFalse(body["ok"])


class StuckAndAnalyzeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(self._tmp.name)

    def test_api_stuck_joins_cache_and_feedback(self):
        a_ans = Action("cse_ans", "ff", ANSWER, "waiting", question="Q?")
        a_rev = Action("cse_rev", "ai", REVIEW, "idle")
        a_skip = Action("cse_skip", "ff", SKIP, "running")
        # seed a cached analysis + feedback for the ANSWER thread
        manager.append_decision(self.cfg, manager.decision_record(
            a_ans, advice={"ok": True, "recommendation": "SESSION: pick X",
                           "rec_manager": "", "rec_session": "pick X",
                           "analysis": "Five sentences."}, ts="T"))
        ui.add_feedback(self.cfg.feedback_file, manager.action_sig(a_ans), "good call")
        code, body = ui.api_stuck(self.cfg, get_token=lambda *a, **k: "tok",
                                  scan=lambda *a, **k: [a_ans, a_rev, a_skip])
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"],
                         {"sessions": 3, "actionable": 2, "analyzed": 1, "stale": 0})
        rows = {r["session_id"]: r for r in body["rows"]}
        self.assertEqual(rows["cse_ans"]["rec_session"], "pick X")    # the session half
        self.assertEqual(rows["cse_ans"]["rec_manager"], "")
        self.assertTrue(rows["cse_ans"]["has_analysis"])
        self.assertEqual(rows["cse_ans"]["feedback"][0]["text"], "good call")
        # enrichment fields present (no local worktree/transcript in the test env)
        self.assertIn("title", rows["cse_ans"])
        self.assertFalse(rows["cse_ans"]["worktree"]["exists"])
        self.assertEqual(rows["cse_ans"]["last_messages"], [])
        # no local transcript -> can't tell -> never flagged stale
        self.assertFalse(rows["cse_ans"]["analysis_stale"])
        self.assertEqual(rows["cse_ans"]["session_updated_at"], "")
        self.assertFalse(rows["cse_rev"]["has_analysis"])
        self.assertFalse(rows["cse_skip"]["actionable"])
        self.assertTrue(body["rows"][0]["actionable"])      # actionable sorted first

    def test_api_stuck_flags_stale_analysis(self):
        """A cached analysis is flagged stale when the session's transcript was
        written after it -- the read no longer reflects the thread."""
        import datetime as _dt
        from pathlib import Path
        a = Action("cse_old", "ff", REVIEW, "idle")
        manager.append_decision(self.cfg, manager.decision_record(
            a, advice={"ok": True, "recommendation": "r", "rec_manager": "",
                       "rec_session": "r", "analysis": "an earlier read."},
            ts="2026-05-25T12:00:00+00:00"))
        tp = os.path.join(self._tmp.name, "t.jsonl")
        with open(tp, "w") as fh:
            fh.write("{}\n")
        later = _dt.datetime(2026, 5, 25, 13, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
        os.utime(tp, (later, later))   # session wrote an hour after the analysis
        with mock.patch("remote_control.manager.session_transcript", return_value=Path(tp)):
            code, body = ui.api_stuck(self.cfg, get_token=lambda *x, **k: "tok",
                                      scan=lambda *x, **k: [a])
        row = body["rows"][0]
        self.assertTrue(row["analysis_stale"])
        self.assertEqual(body["counts"]["stale"], 1)
        self.assertTrue(row["session_updated_at"].startswith("2026-05-25T13:00"))

    def test_api_stuck_no_token(self):
        code, body = ui.api_stuck(self.cfg, get_token=lambda *a, **k: "",
                                  scan=lambda *a, **k: [])
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertIn("token", body["error"])

    def test_run_analysis_writes_record(self):
        os.makedirs(os.path.join(self._tmp.name, "ff"))
        a = Action("cse_1", "ff", REVIEW, "idle", run_dir=os.path.join(self._tmp.name, "ff"))
        with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
            res = ui.run_analysis(self.cfg, "cse_1", get_token=lambda *x, **k: "tok",
                                  runner=lambda *x, **k: _proc(
                                      stdout="ANALYSIS: a.\nMANAGER: close it\nSESSION: none"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["record"]["rec_manager"], "close it")
        # it landed in the decisions log (so the UI table will show it)
        self.assertEqual(len(manager.read_decisions(self.cfg)), 1)

    def test_run_analysis_no_token(self):
        self.assertFalse(ui.run_analysis(self.cfg, "cse_1",
                                         get_token=lambda *x, **k: "")["ok"])

    def test_api_analyze_needs_session_id(self):
        self.assertEqual(ui.api_analyze(self.cfg, {})[0], 400)


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(os.path.join(self._tmp.name, "ff"))

    def _cfg_exec(self, **over):
        env = {"REMOTE_CONTROL_LOGDIR": os.path.join(self._tmp.name, "logs"),
               "REMOTE_CONTROL_DEV": self._tmp.name,
               "MANAGER_ALLOWLIST_FILE": "/nonexistent",
               "MANAGER_GUIDELINES_FILE": os.path.join(self._tmp.name, "g.md")}
        env.update(over)
        return ManagerConfig.from_env(env)

    def _seed(self, cfg, manager_rec):
        a = Action("cse_1", "ff", REVIEW, "idle", run_dir=os.path.join(self._tmp.name, "ff"))
        manager.append_decision(cfg, manager.decision_record(
            a, advice={"ok": True, "rec_manager": manager_rec, "rec_session": "",
                       "recommendation": manager.format_rec(manager_rec, "")}, ts="T"))
        return a

    def test_run_execution_spawns_when_enabled(self):
        cfg = self._cfg_exec(MANAGER_EXECUTE_ENABLED="1")
        a = self._seed(cfg, "archive the finished session")
        seen = {}

        def runner(cmd, cwd=None, **k):
            seen["prompt"] = cmd[-1]
            return _proc(stdout="archived it")

        with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
            res = ui.run_execution(cfg, "cse_1", get_token=lambda *x, **k: "tok",
                                   runner=runner)
        self.assertTrue(res["ok"])
        self.assertTrue(res["ran"])
        self.assertIn("archive the finished session", seen["prompt"])

    def test_run_execution_shadow_when_disabled(self):
        cfg = self._cfg_exec(MANAGER_EXECUTE_ENABLED="0")
        a = self._seed(cfg, "archive the finished session")
        calls = []
        with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
            res = ui.run_execution(cfg, "cse_1", get_token=lambda *x, **k: "tok",
                                   runner=lambda *x, **k: calls.append(1) or _proc())
        self.assertTrue(res["ok"])
        self.assertFalse(res["ran"])          # shadow: no spawn
        self.assertEqual(calls, [])

    def test_run_execution_no_manager_rec(self):
        cfg = self._cfg_exec(MANAGER_EXECUTE_ENABLED="1")
        a = self._seed(cfg, "")               # SESSION-only rec, nothing to execute
        with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
            res = ui.run_execution(cfg, "cse_1", get_token=lambda *x, **k: "tok",
                                   runner=lambda *x, **k: _proc())
        self.assertFalse(res["ok"])
        self.assertIn("no MANAGER recommendation", res["error"])

    def test_api_execute_needs_session_id(self):
        self.assertEqual(ui.api_execute(self._cfg_exec(), {})[0], 400)


class ServerSmokeTest(unittest.TestCase):
    """A real round-trip through the HTTP handler on an ephemeral port."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(self._tmp.name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui.make_handler(self.cfg))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, r.read()

    def _post(self, path, obj):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(obj).encode(),
                                     headers={"content-type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())

    def test_serves_page(self):
        code, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Manager review", body)

    def test_guidelines_and_feedback_flow(self):
        _, body = self._post("/api/guidelines", {"text": "be careful out there"})
        self.assertTrue(body["ok"])
        self.assertEqual(json.loads(self._get("/api/guidelines")[1])["text"],
                         "be careful out there")
        _, body = self._post("/api/feedback", {"sig": "review:cse_1:", "feedback": "nice"})
        self.assertEqual(body["feedback"][-1]["text"], "nice")

    def test_stuck_no_token_is_graceful(self):
        with mock.patch("remote_control.usage_limit.monitor.get_token", return_value=""):
            body = json.loads(self._get("/api/stuck")[1])
        self.assertFalse(body["ok"])
        self.assertEqual(body["rows"], [])


if __name__ == "__main__":
    unittest.main()
