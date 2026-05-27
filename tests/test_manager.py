import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from remote_control.config import ManagerConfig
from remote_control.manager import (
    ANSWER,
    DEFER,
    RESCUE,
    REVIEW,
    SKIP,
    Action,
    RequiredAction,
    action_sig,
    analysis_is_stale,
    analyze_action,
    analyze_session,
    answer_request,
    archive_request,
    classify,
    collect_actions,
    cooldown_ok,
    decision_record,
    do_replay,
    evaluate_answer,
    execute_answer,
    execute_manager_rec,
    executor_prompt,
    feedback_for_prompt,
    format_plan,
    format_questions,
    format_rec,
    get_session_detail,
    idle_seconds,
    investigator_command,
    investigator_prompt,
    investigator_prompt_advise,
    investigator_prompt_answer,
    is_fresh,
    last_assistant_text,
    last_messages,
    latest_decision_by_sig,
    monitor_tick,
    option_labels,
    parse_advice,
    parse_choices,
    parse_required_action,
    plan_for_session,
    post_answer,
    read_decisions,
    read_guidelines,
    record_action,
    run_investigator,
    selection_event_body,
    suggest_guidelines,
    summarize,
    worktree_for,
    worktree_status,
)

NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
NOW_EPOCH = NOW.timestamp()

BASE_ENV = {
    "REMOTE_CONTROL_HOST": "testhost",
    "REMOTE_CONTROL_DEV": "/Users/x/dev",
    "REMOTE_CONTROL_CLAUDE_BIN": "/bin/claude",
    "MANAGER_ANSWER_GRACE_SECS": "600",
    "MANAGER_DONE_GRACE_SECS": "1800",
    "MANAGER_RESCUE_GRACE_SECS": "3600",
    "MANAGER_COOLDOWN_SECS": "1800",
}


def _cfg(**over) -> ManagerConfig:
    env = dict(BASE_ENV)
    env.update({k: str(v) for k, v in over.items()})
    return ManagerConfig.from_env(env)


def _ts(secs_ago: float) -> str:
    return datetime.fromtimestamp(NOW_EPOCH - secs_ago, timezone.utc).isoformat()


class IdleSecondsTest(unittest.TestCase):
    def test_parses_age(self):
        self.assertAlmostEqual(idle_seconds(_ts(120), NOW), 120, delta=1)

    def test_unknown_is_none(self):
        self.assertIsNone(idle_seconds("", NOW))
        self.assertIsNone(idle_seconds("not-a-date", NOW))

    def test_future_clamps_to_zero(self):
        self.assertEqual(idle_seconds(_ts(-300), NOW), 0.0)


class ClassifyTest(unittest.TestCase):
    def _k(self, **kw):
        base = dict(worker_status="idle", connection_status="connected",
                    limit_paused=False, idle_secs=10_000, cfg=_cfg())
        base.update(kw)
        return classify(**base)[0]

    def test_limit_paused_defers_to_skip(self):
        self.assertEqual(self._k(limit_paused=True), SKIP)

    def test_unknown_idle_skips(self):
        self.assertEqual(self._k(idle_secs=None), SKIP)

    def test_requires_action_over_grace_answers(self):
        self.assertEqual(self._k(worker_status="requires_action", idle_secs=700), ANSWER)

    def test_requires_action_under_grace_still_actionable(self):
        # within grace is now actionable (just fresh); grace only gates the auto-loop
        self.assertEqual(self._k(worker_status="requires_action", idle_secs=120), ANSWER)

    def test_disconnected_over_grace_rescues(self):
        self.assertEqual(
            self._k(worker_status="idle", connection_status="disconnected",
                    idle_secs=4000), RESCUE)

    def test_disconnected_under_grace_still_actionable(self):
        self.assertEqual(
            self._k(connection_status="disconnected", idle_secs=120), RESCUE)

    def test_running_recent_skips(self):
        # events still flowing -> trust "running"
        self.assertEqual(self._k(worker_status="running", idle_secs=60), SKIP)

    def test_running_stale_is_actionable(self):
        # "running" but silent past the stuck-running threshold -> caught as REVIEW
        self.assertEqual(self._k(worker_status="running", idle_secs=10_000), REVIEW)

    def test_idle_over_done_grace_reviews(self):
        self.assertEqual(self._k(worker_status="idle", idle_secs=2000), REVIEW)

    def test_idle_under_done_grace_still_actionable(self):
        self.assertEqual(self._k(worker_status="idle", idle_secs=600), REVIEW)

    def test_question_wins_over_disconnect(self):
        # a pending question is higher value than a dead connection
        self.assertEqual(
            self._k(worker_status="requires_action", connection_status="disconnected",
                    idle_secs=10_000), ANSWER)


class IsFreshTest(unittest.TestCase):
    def test_within_grace_is_fresh(self):
        self.assertTrue(is_fresh(ANSWER, 120, _cfg()))      # < 600 answer-grace
        self.assertTrue(is_fresh(REVIEW, 600, _cfg()))      # < 1800 done-grace
        self.assertTrue(is_fresh(RESCUE, 120, _cfg()))      # < 3600 rescue-grace

    def test_past_grace_not_fresh(self):
        self.assertFalse(is_fresh(ANSWER, 700, _cfg()))
        self.assertFalse(is_fresh(REVIEW, 2000, _cfg()))

    def test_skip_and_unknown_not_fresh(self):
        self.assertFalse(is_fresh(SKIP, 10, _cfg()))
        self.assertFalse(is_fresh(REVIEW, None, _cfg()))


class CooldownTest(unittest.TestCase):
    def test_no_entry_ok(self):
        self.assertTrue(cooldown_ok(None, NOW_EPOCH, 1800))
        self.assertTrue(cooldown_ok({}, NOW_EPOCH, 1800))

    def test_recent_action_blocks(self):
        self.assertFalse(cooldown_ok({"last_action_at": NOW_EPOCH - 60}, NOW_EPOCH, 1800))

    def test_old_action_ok(self):
        self.assertTrue(cooldown_ok({"last_action_at": NOW_EPOCH - 3600}, NOW_EPOCH, 1800))


class LastAssistantTextTest(unittest.TestCase):
    def test_list_content_blocks(self):
        lines = [json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "Which DB should I use?"}]}})]
        self.assertEqual(last_assistant_text(lines), "Which DB should I use?")

    def test_string_content(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": "go ahead?"}})]
        self.assertEqual(last_assistant_text(lines), "go ahead?")

    def test_picks_newest_assistant_turn(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": "first"}}),
            json.dumps({"type": "user", "message": {"content": "reply"}}),
            json.dumps({"type": "assistant", "message": {"content": "latest?"}}),
        ]
        self.assertEqual(last_assistant_text(lines), "latest?")

    def test_tolerates_junk_and_blanks(self):
        lines = ["", "not json", json.dumps({"type": "assistant",
                                             "message": {"content": "ok?"}})]
        self.assertEqual(last_assistant_text(lines), "ok?")

    def test_none_when_no_assistant_text(self):
        self.assertIsNone(last_assistant_text([json.dumps(
            {"type": "user", "message": {"content": "hi"}})]))
        self.assertIsNone(last_assistant_text([]))


class PromptAndCommandTest(unittest.TestCase):
    def test_answer_prompt_includes_question(self):
        p = investigator_prompt(ANSWER, repo="ff", run_dir="/d", question="Pick A or B?")
        self.assertIn("Pick A or B?", p)
        self.assertIn("ONLY the answer", p)

    def test_answer_prompt_handles_missing_question(self):
        p = investigator_prompt(ANSWER, repo="ff", run_dir="/d", question=None)
        self.assertIn("could not read the pending question", p)

    def test_review_prompt_has_done_next_contract(self):
        p = investigator_prompt(REVIEW, repo="ff", run_dir="/d", question=None)
        self.assertIn("DONE:", p)
        self.assertIn("NEXT:", p)
        self.assertIn("/close-work", p)

    def test_command_default_is_plan_mode(self):
        # read-only exploration in the paused session's shared worktree
        self.assertEqual(investigator_command("/bin/claude", "hi", None),
                         ["/bin/claude", "-p", "--permission-mode", "plan", "hi"])

    def test_command_with_model(self):
        self.assertEqual(
            investigator_command("/bin/claude", "hi", "sonnet"),
            ["/bin/claude", "-p", "--permission-mode", "plan", "--model", "sonnet", "hi"])

    def test_command_empty_permission_mode_omits_flag(self):
        self.assertEqual(
            investigator_command("/bin/claude", "hi", None, permission_mode=""),
            ["/bin/claude", "-p", "hi"])

    def test_command_is_not_accept_edits(self):
        # investigator must not silently edit the worktree it shares
        self.assertNotIn("acceptEdits", investigator_command("/bin/claude", "hi", None))


class RequestShapeTest(unittest.TestCase):
    def test_answer_request_is_user_event(self):
        method, path, body = answer_request("cse_1", "use postgres")
        self.assertEqual((method, path), ("POST", "/sessions/cse_1/events"))
        ev = body["events"][0]
        self.assertEqual(ev["payload"]["message"]["content"][0]["text"], "use postgres")

    def test_archive_request(self):
        # Verified live: POST /sessions/{id}/archive (PUT returns 405).
        self.assertEqual(archive_request("cse_1"),
                         ("POST", "/sessions/cse_1/archive", {}))


def _sess(**kw) -> dict:
    base = dict(id="cse_1", status="active", worker_status="idle",
                connection_status="connected", last_event_at=_ts(10_000),
                environment_kind="bridge")
    base.update(kw)
    return base


def _plan(s, *, repo="ai-harness", managed=True, state=None, cfg=None) -> Action:
    return plan_for_session(
        s, cfg=cfg or _cfg(), repo=repo, managed=managed, now=NOW,
        now_epoch=NOW_EPOCH, state=state or {"sessions": {}})


class PlanForSessionTest(unittest.TestCase):
    def test_limit_paused_defers(self):
        s = _sess(worker_status="idle", post_turn_summary={
            "status_category": "failed", "status_detail": "usage limit (5h 99%)"})
        self.assertEqual(_plan(s).kind, DEFER)

    def test_recent_idle_is_fresh_review(self):
        a = _plan(_sess(last_event_at=_ts(60)))   # idle, within done-grace
        self.assertEqual(a.kind, REVIEW)          # actionable now...
        self.assertTrue(a.fresh)                  # ...just flagged within-grace

    def test_idle_done_reviews_with_command(self):
        a = _plan(_sess(worker_status="idle", last_event_at=_ts(10_000)))
        self.assertEqual(a.kind, REVIEW)
        self.assertTrue(a.command and a.command[0] == "/bin/claude")
        self.assertIn("/sessions/cse_1/events", a.api)

    def test_waiting_question_answers(self):
        a = _plan(_sess(worker_status="requires_action", last_event_at=_ts(10_000)))
        self.assertEqual(a.kind, ANSWER)
        # no local transcript in the test env -> run_dir falls back to <dev>/<repo>
        self.assertEqual(a.run_dir, "/Users/x/dev/ai-harness")

    def test_disconnected_rescues(self):
        a = _plan(_sess(connection_status="disconnected", last_event_at=_ts(10_000)))
        self.assertEqual(a.kind, RESCUE)
        self.assertIn("archive", a.api)

    def test_cooldown_skips_actionable(self):
        state = {"sessions": {"cse_1": {"last_action_at": NOW_EPOCH - 60}}}
        a = _plan(_sess(worker_status="requires_action", last_event_at=_ts(10_000)),
                  state=state)
        self.assertEqual(a.kind, SKIP)
        self.assertIn("cooldown", a.reason)

    def test_unmanaged_actionable_reported_not_acted(self):
        a = _plan(_sess(worker_status="requires_action", last_event_at=_ts(10_000)),
                  managed=False)
        self.assertEqual(a.kind, ANSWER)       # state is reported truthfully
        self.assertFalse(a.managed)            # but it's flagged unmanaged
        self.assertFalse(a.command)            # and no plan is built
        self.assertIn("allowlist", a.reason)

    def test_unmanaged_fresh_reported_not_acted(self):
        a = _plan(_sess(last_event_at=_ts(60)), managed=False)
        self.assertEqual(a.kind, REVIEW)   # reported truthfully (within grace)
        self.assertFalse(a.managed)        # but flagged unmanaged
        self.assertTrue(a.fresh)


class RenderTest(unittest.TestCase):
    def test_format_plan_empty(self):
        self.assertEqual(format_plan([], "host", True), "(no sessions)")

    def test_format_plan_shows_verb_and_kind(self):
        a = _plan(_sess(worker_status="requires_action", last_event_at=_ts(10_000)))
        text = format_plan([a], "host", dry_run=True)
        self.assertIn("[ANSWER", text)
        self.assertIn("WOULD run investigator", text)

    def test_format_plan_live_verb(self):
        a = _plan(_sess(connection_status="disconnected", last_event_at=_ts(10_000)))
        self.assertIn("WILL", format_plan([a], "host", dry_run=False))

    def test_unmanaged_tag(self):
        a = _plan(_sess(last_event_at=_ts(60)), managed=False)
        self.assertIn("[UNMANAGED", format_plan([a], "host", True))

    def test_summarize_counts(self):
        rows = [
            _plan(_sess(id="a", worker_status="requires_action", last_event_at=_ts(9999))),
            _plan(_sess(id="b", last_event_at=_ts(60))),
        ]
        out = summarize(rows)
        self.assertIn("answer×1", out)
        self.assertIn("review×1", out)   # idle 60s is now an actionable review, not skip


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class RunInvestigatorTest(unittest.TestCase):
    def test_returns_trimmed_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            out = run_investigator(
                ["x"], d, 5, runner=lambda *a, **k: _proc(stdout="  use postgres \n"),
                log=lambda m: None)
        self.assertEqual(out, "use postgres")

    def test_missing_dir_is_none(self):
        self.assertIsNone(run_investigator(
            ["x"], "/no/such/dir", 5, runner=lambda *a, **k: _proc(stdout="hi"),
            log=lambda m: None))

    def test_nonzero_rc_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(run_investigator(
                ["x"], d, 5, runner=lambda *a, **k: _proc(returncode=1, stderr="boom"),
                log=lambda m: None))

    def test_empty_stdout_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(run_investigator(
                ["x"], d, 5, runner=lambda *a, **k: _proc(stdout="   \n"),
                log=lambda m: None))

    def test_timeout_is_none(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=5)
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(run_investigator(["x"], d, 5, runner=boom, log=lambda m: None))


class PostAnswerTest(unittest.TestCase):
    def test_posts_user_event_on_success(self):
        calls = []

        def api(cfg, method, path, token, body=None):
            calls.append((method, path, body))
            return 200, {}

        ok = post_answer(None, "tok", "cse_1", "use postgres", api=api, log=lambda m: None)
        self.assertTrue(ok)
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "/sessions/cse_1/events"))
        self.assertEqual(
            body["events"][0]["payload"]["message"]["content"][0]["text"], "use postgres")

    def test_failure_returns_false(self):
        ok = post_answer(None, "tok", "cse_1", "x",
                         api=lambda *a, **k: (400, "bad"), log=lambda m: None)
        self.assertFalse(ok)


class RecordActionTest(unittest.TestCase):
    def test_stamps_and_increments(self):
        state = {"sessions": {}}
        record_action(state, "cse_1", ANSWER, NOW_EPOCH, question="Q?")
        ent = state["sessions"]["cse_1"]
        self.assertEqual(ent["attempts"], 1)
        self.assertEqual(ent["last_action_at"], NOW_EPOCH)
        record_action(state, "cse_1", ANSWER, NOW_EPOCH + 1, question="Q?")
        self.assertEqual(state["sessions"]["cse_1"]["attempts"], 2)

    def test_recorded_action_then_blocks_cooldown(self):
        state = {"sessions": {}}
        record_action(state, "cse_1", ANSWER, NOW_EPOCH)
        self.assertFalse(cooldown_ok(state["sessions"]["cse_1"], NOW_EPOCH + 60, 1800))


def _ra(tool_use_id="toolu_1", multi=False, n=1) -> RequiredAction:
    qs = [{
        "header": "Scope", "question": "How to handle repos?", "multiSelect": multi,
        "options": [{"label": "Skip them", "description": "skip"},
                    {"label": "Label first", "description": "label"}],
    }]
    if n == 2:
        qs.append({"header": "Apply", "question": "Apply now?", "multiSelect": False,
                   "options": [{"label": "Apply as shown", "description": "go"},
                               {"label": "Let me adjust", "description": "wait"}]})
    return RequiredAction(tool_name="AskUserQuestion", tool_use_id=tool_use_id,
                          description="d", questions=qs)


class RequiredActionParseTest(unittest.TestCase):
    def test_parses_structure(self):
        detail = {"requires_action_details": {
            "tool_name": "AskUserQuestion", "tool_use_id": "toolu_9",
            "action_description": "pick", "input": {"questions": [
                {"header": "H", "question": "Q?", "multiSelect": False,
                 "options": [{"label": "A", "description": "a"}]}]}}}
        ra = parse_required_action(detail)
        self.assertEqual(ra.tool_name, "AskUserQuestion")
        self.assertEqual(ra.tool_use_id, "toolu_9")
        self.assertEqual(len(ra.questions), 1)

    def test_none_when_absent(self):
        self.assertIsNone(parse_required_action({}))
        self.assertIsNone(parse_required_action(None))

    def test_missing_questions_is_empty_list(self):
        ra = parse_required_action({"requires_action_details": {"tool_name": "X"}})
        self.assertEqual(ra.questions, [])


class QuestionFormatTest(unittest.TestCase):
    def test_option_labels(self):
        self.assertEqual(option_labels(_ra().questions[0]), ["Skip them", "Label first"])

    def test_format_questions_lists_options(self):
        text = format_questions(_ra(n=2, multi=True))
        self.assertIn("Q1 [Scope]: How to handle repos?", text)
        self.assertIn("(multi-select)", text)
        self.assertIn("- Skip them: skip", text)
        self.assertIn("Q2 [Apply]: Apply now?", text)

    def test_investigator_prompt_answer_has_options_and_format(self):
        p = investigator_prompt_answer(_ra(), repo="ff", run_dir="/d")
        self.assertIn("Skip them", p)
        self.assertIn("Q<number>:", p)


class ParseChoicesTest(unittest.TestCase):
    def test_valid_choice(self):
        sel = parse_choices("Q1: Skip them", _ra())
        self.assertEqual(sel, [{"header": "Scope", "question": "How to handle repos?",
                                "label": "Skip them"}])

    def test_case_insensitive_match(self):
        self.assertEqual(parse_choices("Q1: skip them", _ra())[0]["label"], "Skip them")

    def test_separator_variants(self):
        self.assertIsNotNone(parse_choices("Q1) Skip them", _ra()))
        self.assertIsNotNone(parse_choices("Q1. Skip them", _ra()))

    def test_unmappable_label_is_none(self):
        self.assertIsNone(parse_choices("Q1: Banana", _ra()))

    def test_missing_question_is_none(self):
        self.assertIsNone(parse_choices("Q1: Skip them", _ra(n=2)))  # Q2 unanswered

    def test_two_questions_both_answered(self):
        sel = parse_choices("Q1: Skip them\nQ2: Apply as shown", _ra(n=2))
        self.assertEqual([s["label"] for s in sel], ["Skip them", "Apply as shown"])

    def test_extra_noise_lines_ignored(self):
        sel = parse_choices("here goes\nQ1: Label first\nthanks", _ra())
        self.assertEqual(sel[0]["label"], "Label first")


class SelectionBodyTest(unittest.TestCase):
    def test_tool_result_shape(self):
        body = selection_event_body(_ra(tool_use_id="toolu_7"),
                                    [{"header": "Scope", "label": "Skip them"}])
        block = body["events"][0]["payload"]["message"]["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "toolu_7")
        self.assertIn("Skip them", block["content"][0]["text"])


class GetSessionDetailTest(unittest.TestCase):
    def test_unwraps_response_shape(self):
        d = get_session_detail(None, "t", "cse_1",
                               api=lambda *a, **k: (200, {"response_shape": {"id": "x"}}))
        self.assertEqual(d, {"id": "x"})

    def test_returns_data_without_response_shape(self):
        d = get_session_detail(None, "t", "cse_1", api=lambda *a, **k: (200, {"id": "y"}))
        self.assertEqual(d, {"id": "y"})

    def test_none_on_error(self):
        self.assertIsNone(get_session_detail(None, "t", "cse_1",
                                             api=lambda *a, **k: (404, "nope")))


class ExecuteAnswerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _action(self, required=None):
        return Action("cse_1", "ai-harness", ANSWER, "waiting",
                      run_dir=self._tmp.name, question="Q1...",
                      command=["claude", "-p", "x"], api="POST ...",
                      required=required if required is not None else _ra())

    def test_submits_tool_result_when_enabled(self):
        posted = []
        ok = execute_answer(
            self._action(), ucfg=None, token="t", cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"),
            state={"sessions": {}}, now_epoch=NOW_EPOCH, log=lambda m: None,
            runner=lambda *a, **k: _proc(stdout="Q1: Skip them"),
            api=lambda c, m, p, tok, body=None: (posted.append((m, p, body)) or (200, {})))
        self.assertTrue(ok)
        m, p, body = posted[0]
        self.assertEqual((m, p), ("POST", "/sessions/cse_1/events"))
        block = body["events"][0]["payload"]["message"]["content"][0]
        self.assertEqual(block["type"], "tool_result")
        self.assertEqual(block["tool_use_id"], "toolu_1")
        self.assertIn("Skip them", block["content"][0]["text"])

    def test_records_state_when_submitted(self):
        state = {"sessions": {}}
        execute_answer(self._action(), ucfg=None, token="t",
                       cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"), state=state,
                       now_epoch=NOW_EPOCH, log=lambda m: None,
                       runner=lambda *a, **k: _proc(stdout="Q1: Skip them"),
                       api=lambda *a, **k: (200, {}))
        self.assertIn("cse_1", state["sessions"])

    def test_submission_disabled_investigates_but_does_not_post(self):
        calls = []
        ok = execute_answer(
            self._action(), ucfg=None, token="t", cfg=_cfg(),  # submit off (default)
            state={"sessions": {}}, now_epoch=NOW_EPOCH, log=lambda m: None,
            runner=lambda *a, **k: _proc(stdout="Q1: Skip them"),
            api=lambda *a, **k: (calls.append(1) or (200, {})))
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_unmappable_choice_not_submitted(self):
        calls = []
        ok = execute_answer(
            self._action(), ucfg=None, token="t", cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"),
            state={"sessions": {}}, now_epoch=NOW_EPOCH, log=lambda m: None,
            runner=lambda *a, **k: _proc(stdout="Q1: Banana"),
            api=lambda *a, **k: (calls.append(1) or (200, {})))
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_no_choice_output_skips(self):
        ok = execute_answer(
            self._action(), ucfg=None, token="t", cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"),
            state={"sessions": {}}, now_epoch=NOW_EPOCH, log=lambda m: None,
            runner=lambda *a, **k: _proc(stdout="   "), api=lambda *a, **k: (200, {}))
        self.assertFalse(ok)

    def test_no_required_question_skips(self):
        a = self._action(required=RequiredAction("X", "t", "", []))
        ok = execute_answer(a, ucfg=None, token="t", cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"),
                            state={"sessions": {}}, now_epoch=NOW_EPOCH, log=lambda m: None,
                            runner=lambda *a, **k: _proc(stdout="Q1: Skip them"),
                            api=lambda *a, **k: (200, {}))
        self.assertFalse(ok)

    def test_submit_failure_not_recorded(self):
        state = {"sessions": {}}
        ok = execute_answer(self._action(), ucfg=None, token="t",
                            cfg=_cfg(MANAGER_SUBMIT_ENABLED="1"), state=state,
                            now_epoch=NOW_EPOCH, log=lambda m: None,
                            runner=lambda *a, **k: _proc(stdout="Q1: Skip them"),
                            api=lambda *a, **k: (500, "err"))
        self.assertFalse(ok)
        self.assertEqual(state["sessions"], {})


class DecisionRecordTest(unittest.TestCase):
    def test_with_advice(self):
        a = Action("cse_1", "r", ANSWER, "why", required=_ra())
        rec = decision_record(a, advice={
            "ok": True, "recommendation": "SESSION: pick: Skip them",
            "rec_manager": "", "rec_session": "pick: Skip them",
            "analysis": "Five sentences here.", "note": "analyzed"}, ts="T")
        self.assertEqual(rec["case"], ANSWER)
        self.assertTrue(rec["analyzed"])
        self.assertEqual(rec["rec_session"], "pick: Skip them")
        self.assertEqual(rec["rec_manager"], "")
        self.assertEqual(rec["recommendation"], "SESSION: pick: Skip them")
        self.assertEqual(rec["analysis"], "Five sentences here.")
        self.assertEqual(rec["questions"][0]["options"], ["Skip them", "Label first"])
        self.assertEqual(rec["session_id"], "cse_1")
        self.assertTrue(rec["sig"].startswith("answer:cse_1:"))

    def test_without_advice(self):
        rec = decision_record(Action("cse_1", "r", ANSWER, "why", required=_ra()),
                              advice={"ok": False, "note": "no output"}, ts="T")
        self.assertFalse(rec["analyzed"])
        self.assertEqual(rec["recommendation"], "")
        self.assertEqual(rec["note"], "no output")


class AdviseTest(unittest.TestCase):
    def test_parse_advice_sections(self):
        # /close-work is a SESSION step; archive is the MANAGER follow-up
        m, s, ana = parse_advice(
            "ANALYSIS: because Y. and Z.\nMANAGER: archive the session\nSESSION: run /close-work")
        self.assertEqual(m, "archive the session")
        self.assertEqual(s, "run /close-work")
        self.assertTrue(ana.startswith("because Y"))

    def test_parse_advice_multiline_and_none(self):
        # multi-line sections (sequential steps) + a "none" half drops to ""
        m, s, ana = parse_advice(
            "MANAGER: none\nSESSION: step one\nstep two\nANALYSIS: a.")
        self.assertEqual(m, "")
        self.assertEqual(s, "step one\nstep two")
        self.assertEqual(ana, "a.")

    def test_parse_advice_justified_none(self):
        # the model often tacks a reason onto "none" -- still treat the half as empty
        m, s, _ = parse_advice(
            "MANAGER: none -- already delivered\nSESSION: none.\nANALYSIS: a.")
        self.assertEqual((m, s), ("", ""))
        # but "none of ..." is real content, not the empty marker
        m2, _, _ = parse_advice("MANAGER: none of the work is committed yet\nSESSION: none")
        self.assertEqual(m2, "none of the work is committed yet")

    def test_parse_advice_legacy_and_fallback(self):
        # legacy single RECOMMENDATION -> the SESSION half
        m, s, _ = parse_advice("RECOMMENDATION: do X\nANALYSIS: y.")
        self.assertEqual((m, s), ("", "do X"))
        # unlabelled prose -> a SESSION rec (never a MANAGER one) + analysis
        self.assertEqual(parse_advice("just prose, no labels"),
                         ("", "just prose, no labels", "just prose, no labels"))
        self.assertEqual(parse_advice(""), ("", "", ""))

    def test_format_rec_combines_present_halves(self):
        self.assertEqual(format_rec("archive the session", "run /close-work"),
                         "MANAGER: archive the session\nSESSION: run /close-work")
        self.assertEqual(format_rec("", "answer it"), "SESSION: answer it")
        self.assertEqual(format_rec("", ""), "")

    def test_action_sig(self):
        ans = action_sig(Action("cse_1", "r", ANSWER, "w", question="Q?"))
        self.assertTrue(ans.startswith("answer:cse_1:"))
        self.assertNotEqual(ans, action_sig(Action("cse_1", "r", ANSWER, "w",
                                                    question="other")))
        self.assertEqual(action_sig(Action("cse_1", "r", REVIEW, "w")), "review:cse_1:")

    def test_read_guidelines_missing_and_present(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(read_guidelines(
                _cfg(MANAGER_GUIDELINES_FILE=os.path.join(d, "none.md"))), "")
            p = os.path.join(d, "g.md")
            with open(p, "w") as fh:
                fh.write("NEVER force-push")
            self.assertEqual(read_guidelines(_cfg(MANAGER_GUIDELINES_FILE=p)),
                             "NEVER force-push")

    def test_prompt_advise_injects_guidelines(self):
        p = investigator_prompt_advise(Action("cse_1", "ff", REVIEW, "idle", run_dir="/d"),
                                       guidelines="NEVER force-push")
        self.assertIn("NEVER force-push", p)
        self.assertIn("MANAGER:", p)
        self.assertIn("SESSION:", p)

    def test_prompt_advise_omits_transcript_section_by_default(self):
        # Live manager path: no transcript_tail -> no RECENT TURNS section, so
        # current behavior is preserved.
        p = investigator_prompt_advise(Action("cse_1", "ff", REVIEW, "idle", run_dir="/d"))
        self.assertNotIn("RECENT TURNS", p)

    def test_prompt_advise_includes_transcript_tail_when_provided(self):
        # Eval path: a frozen tail is inlined so the prompt is self-contained.
        tail = [{"role": "assistant", "text": "what should I do about X?"},
                {"role": "user", "text": "try Y"}]
        p = investigator_prompt_advise(Action("cse_1", "ff", REVIEW, "idle", run_dir="/d"),
                                       transcript_tail=tail)
        self.assertIn("RECENT TURNS", p)
        self.assertIn("[assistant] what should I do about X?", p)
        self.assertIn("[user] try Y", p)
        # Empty turns are skipped so we don't leak blank role tags.
        p2 = investigator_prompt_advise(
            Action("cse_1", "ff", REVIEW, "idle", run_dir="/d"),
            transcript_tail=[{"role": "assistant", "text": "  "},
                             {"role": "user", "text": "real"}])
        self.assertNotIn("[assistant]", p2)
        self.assertIn("[user] real", p2)

    def test_analyze_action_forwards_transcript_tail_to_prompt(self):
        # The optional tail flows from analyze_action -> investigator_prompt_advise
        # -> the spawned subprocess command, so the eval runner can replay
        # without depending on the on-disk transcript.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "r"))
            cfg = _cfg(REMOTE_CONTROL_DEV=d)
            a = Action("cse_1", "r", REVIEW, "idle", run_dir=os.path.join(d, "r"))
            seen = {}

            def runner(cmd, **k):
                seen["prompt"] = cmd[-1]
                return _proc(stdout="ANALYSIS: a.\nMANAGER: none\nSESSION: r")

            analyze_action(a, cfg=cfg, guidelines="", log=lambda m: None,
                           runner=runner,
                           transcript_tail=[{"role": "assistant",
                                             "text": "FROZEN-TAIL-MARKER"}])
            self.assertIn("RECENT TURNS", seen["prompt"])
            self.assertIn("FROZEN-TAIL-MARKER", seen["prompt"])

    def test_analyze_action_falls_back_to_dev_when_no_repo(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(REMOTE_CONTROL_DEV=d)
            a = Action("cse_x", None, REVIEW, "idle")   # repo None -> run_dir ""
            seen = {}

            def runner(cmd, cwd=None, **k):
                seen["cwd"] = cwd
                return _proc(stdout="ANALYSIS: a.\nMANAGER: none\nSESSION: r")

            adv = analyze_action(a, cfg=cfg, guidelines="", log=lambda m: None, runner=runner)
            self.assertTrue(adv["ok"])              # ran instead of silently failing
            self.assertEqual(seen["cwd"], d)        # fell back to the dev root

    def test_analyze_action_parses_and_empty(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "ff"))
            cfg = _cfg(REMOTE_CONTROL_DEV=d)
            a = Action("cse_1", "ff", REVIEW, "idle", run_dir=os.path.join(d, "ff"))
            adv = analyze_action(a, cfg=cfg, guidelines="", log=lambda m: None,
                                 runner=lambda *x, **k: _proc(
                                     stdout="ANALYSIS: done.\nMANAGER: close it\nSESSION: none"))
            self.assertTrue(adv["ok"])
            self.assertEqual(adv["rec_manager"], "close it")
            self.assertEqual(adv["rec_session"], "")
            self.assertIn("MANAGER: close it", adv["recommendation"])
            adv2 = analyze_action(a, cfg=cfg, guidelines="", log=lambda m: None,
                                  runner=lambda *x, **k: _proc(stdout="  "))
            self.assertFalse(adv2["ok"])

    def test_read_decisions_latest_by_sig(self):
        with tempfile.TemporaryDirectory() as d:
            from remote_control.manager import append_decision
            cfg = _cfg(REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"))
            a = Action("cse_1", "r", REVIEW, "idle")
            append_decision(cfg, decision_record(a, advice={"ok": True, "recommendation": "old",
                                                            "analysis": "a"}, ts="T1"))
            append_decision(cfg, decision_record(a, advice={"ok": True, "recommendation": "new",
                                                            "analysis": "a"}, ts="T2"))
            self.assertEqual(len(read_decisions(cfg)), 2)
            self.assertEqual(latest_decision_by_sig(cfg)[action_sig(a)]["recommendation"], "new")

    def test_analyze_session_caches_and_force(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "ff"))
            cfg = _cfg(REMOTE_CONTROL_DEV=d, REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"))
            a = Action("cse_1", "ff", REVIEW, "idle", run_dir=os.path.join(d, "ff"))
            calls = []

            def runner(*x, **k):
                calls.append(1)
                return _proc(stdout="ANALYSIS: a.\nMANAGER: none\nSESSION: r")

            with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
                rec1 = analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                       log=lambda m: None, runner=runner)
                self.assertEqual(rec1["rec_session"], "r")
                analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                log=lambda m: None, runner=runner)          # cached
                self.assertEqual(len(calls), 1)
                analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                force=True, log=lambda m: None, runner=runner)  # forced
                self.assertEqual(len(calls), 2)

    def test_analyze_session_reruns_when_session_updated(self):
        """A cached analysis the session has since outgrown is NOT reused: a
        transcript written after the analysis re-runs the investigator."""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "ff"))
            cfg = _cfg(REMOTE_CONTROL_DEV=d, REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"))
            a = Action("cse_1", "ff", REVIEW, "idle", run_dir=os.path.join(d, "ff"))
            calls = []

            def runner(*x, **k):
                calls.append(1)
                return _proc(stdout="ANALYSIS: a.\nMANAGER: none\nSESSION: r")

            with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
                # no local transcript -> can't tell -> cache is reused (not stale)
                with mock.patch("remote_control.manager.session_mtime", return_value=None):
                    analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                    log=lambda m: None, runner=runner)
                    analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                    log=lambda m: None, runner=runner)          # cached
                    self.assertEqual(len(calls), 1)
                # session wrote after the analysis -> stale -> re-run
                with mock.patch("remote_control.manager.session_mtime",
                                return_value=NOW_EPOCH + 100):
                    analyze_session(cfg, None, "tok", "cse_1", now=NOW, guidelines="",
                                    log=lambda m: None, runner=runner)
                    self.assertEqual(len(calls), 2)

    def test_analyze_session_unknown_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"))
            with mock.patch("remote_control.manager.scan_actions",
                            return_value=[Action("cse_x", "r", SKIP, "running")]):
                # a session not in the scan -> nothing to analyze
                self.assertIsNone(analyze_session(cfg, None, "tok", "cse_MISSING",
                                                  now=NOW, guidelines="", log=lambda m: None))

    def test_analyze_session_allows_non_actionable(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "r"))
            cfg = _cfg(REMOTE_CONTROL_DEV=d, REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"))
            a = Action("cse_x", "r", SKIP, "worker running", run_dir=os.path.join(d, "r"))
            with mock.patch("remote_control.manager.scan_actions", return_value=[a]):
                rec = analyze_session(cfg, None, "tok", "cse_x", now=NOW, guidelines="",
                                      log=lambda m: None,
                                      runner=lambda *x, **k: _proc(
                                          stdout="ANALYSIS: busy.\nMANAGER: none\nSESSION: leave it"))
            self.assertEqual(rec["rec_session"], "leave it")


class EvaluateAnswerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _a(self, required=None):
        return Action("cse_1", "r", ANSWER, "w", run_dir=self._tmp.name,
                      command=["claude", "-p", "x"],
                      required=required if required is not None else _ra())

    def test_good_choice(self):
        sel, note = evaluate_answer(self._a(), cfg=_cfg(), log=lambda m: None,
                                    runner=lambda *a, **k: _proc(stdout="Q1: Skip them"))
        self.assertEqual(note, "evaluated")
        self.assertEqual(sel[0]["label"], "Skip them")

    def test_no_output(self):
        sel, note = evaluate_answer(self._a(), cfg=_cfg(), log=lambda m: None,
                                    runner=lambda *a, **k: _proc(stdout="  "))
        self.assertIsNone(sel)

    def test_unmappable(self):
        sel, _ = evaluate_answer(self._a(), cfg=_cfg(), log=lambda m: None,
                                 runner=lambda *a, **k: _proc(stdout="Q1: Banana"))
        self.assertIsNone(sel)

    def test_no_required(self):
        sel, note = evaluate_answer(self._a(required=RequiredAction("X", "t", "", [])),
                                    cfg=_cfg(), log=lambda m: None,
                                    runner=lambda *a, **k: _proc(stdout="Q1: Skip them"))
        self.assertIsNone(sel)
        self.assertIn("no structured question", note)


def _detail_with_question():
    return {"requires_action_details": {
        "tool_name": "AskUserQuestion", "tool_use_id": "t1", "input": {"questions": [
            {"header": "H", "question": "Q?", "multiSelect": False,
             "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}]}}}


class CollectActionsTest(unittest.TestCase):
    def _sessions(self):
        return [
            dict(id="cse_a", status="active", worker_status="requires_action",
                 connection_status="connected", last_event_at=_ts(10_000)),
            dict(id="cse_b", status="archived", worker_status="idle",
                 connection_status="connected", last_event_at=_ts(10_000)),
        ]

    def _collect(self, **over):
        kw = dict(cfg=_cfg(MANAGER_ALLOWLIST_FILE="/nonexistent"), ucfg=None, token="t",
                  index={}, allow={}, consider_all=True, want_repo=None, now=NOW,
                  now_epoch=NOW_EPOCH, state={"sessions": {}}, log=lambda m: None,
                  api=lambda *a, **k: (200, {"response_shape": _detail_with_question()}))
        kw.update(over)
        return collect_actions(self._sessions(), **kw)

    def test_skips_archived_and_fetches_required(self):
        acts = self._collect()
        self.assertEqual(len(acts), 1)              # archived dropped
        self.assertEqual(acts[0].kind, ANSWER)
        self.assertIsNotNone(acts[0].required)

    def test_want_repo_filters_out_unmatched(self):
        self.assertEqual(self._collect(want_repo="nomatch"), [])

    def test_skip_ids_excluded(self):
        acts = self._collect(cfg=_cfg(MANAGER_ALLOWLIST_FILE="/nonexistent",
                                      MANAGER_SKIP_SIDS="cse_a"))
        self.assertEqual(acts, [])


class ExecuteManagerRecTest(unittest.TestCase):
    def _a(self, run_dir):
        return Action("cse_1", "ff", REVIEW, "idle", run_dir=run_dir)

    def test_shadow_when_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(MANAGER_EXECUTE_ENABLED="0")
            calls = []
            res = execute_manager_rec(self._a(d), cfg=cfg,
                                      manager_rec="archive the finished session",
                                      log=lambda m: None,
                                      runner=lambda *a, **k: calls.append(1) or _proc())
            self.assertTrue(res["ok"])
            self.assertFalse(res["ran"])         # shadow: never spawned
            self.assertEqual(calls, [])

    def test_spawns_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(MANAGER_EXECUTE_ENABLED="1")
            seen = {}

            def runner(cmd, cwd=None, **k):
                seen["cwd"], seen["prompt"] = cwd, cmd[-1]
                return _proc(stdout="closed it")

            res = execute_manager_rec(self._a(d), cfg=cfg,
                                      manager_rec="archive the finished session",
                                      guidelines="be careful", log=lambda m: None,
                                      runner=runner)
            self.assertTrue(res["ran"])
            self.assertEqual(res["output"], "closed it")
            self.assertEqual(seen["cwd"], d)
            self.assertIn("archive the finished session", seen["prompt"])
            self.assertIn("be careful", seen["prompt"])      # guidelines injected
            self.assertIn("EXECUTOR", seen["prompt"])

    def test_no_manager_rec_is_noop(self):
        res = execute_manager_rec(Action("cse_1", "ff", REVIEW, "x"),
                                  cfg=_cfg(MANAGER_EXECUTE_ENABLED="1"), manager_rec="  ",
                                  log=lambda m: None, runner=lambda *a, **k: _proc())
        self.assertFalse(res["ran"])
        self.assertEqual(res["note"], "no manager action")

    def test_executor_runs_writeable_by_default(self):
        # the executor must be able to act -- not the read-only `plan` investigator
        cfg = _cfg()
        self.assertEqual(cfg.executor_permission_mode, "acceptEdits")
        self.assertFalse(cfg.execute_enabled)            # off until trusted

    def test_executor_prompt_carries_rec(self):
        p = executor_prompt(Action("cse_9", "ff", REVIEW, "idle", run_dir="/d"),
                            "fork + resume + archive")
        self.assertIn("fork + resume + archive", p)
        self.assertIn("cse_9", p)


class MonitorTickTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # a real <dev>/<repo> so the investigator's run_dir exists
        os.makedirs(os.path.join(self._tmp.name, "myrepo"))
        self.cfg = _cfg(REMOTE_CONTROL_DEV=self._tmp.name,
                        REMOTE_CONTROL_LOGDIR=os.path.join(self._tmp.name, "logs"),
                        MANAGER_GUIDELINES_FILE=os.path.join(self._tmp.name, "none.md"),
                        MANAGER_ALLOWLIST_FILE="/nonexistent")

    def test_analyzes_and_persists_decision(self):
        sessions = [dict(id="cse_a", status="active", worker_status="requires_action",
                         connection_status="connected", environment_kind="bridge",
                         last_event_at=_ts(10_000))]
        api = lambda *a, **k: (200, {"response_shape": _detail_with_question()})
        advise = "ANALYSIS: It is safe. More here.\nMANAGER: none\nSESSION: pick Skip them"
        with mock.patch("remote_control.usage_limit.monitor.list_sessions",
                        return_value=sessions), \
             mock.patch("remote_control.manager.build_worktree_index",
                        return_value={"cse_a": "myrepo"}):
            recs = monitor_tick(self.cfg, None, "tok", now=NOW, log=lambda m: None,
                                consider_all=True,
                                runner=lambda *a, **k: _proc(stdout=advise), api=api)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["rec_session"], "pick Skip them")
        self.assertEqual(recs[0]["rec_manager"], "")        # nothing for the manager to run
        self.assertNotIn("executed", recs[0])               # so no execution attempted
        self.assertTrue(recs[0]["analysis"].startswith("It is safe"))
        self.assertTrue(recs[0]["analyzed"])
        lines = self.cfg.decisions_file.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["session_id"], "cse_a")
        self.assertTrue(list(self.cfg.scenarios_dir.glob("cse_a-*.json")))  # scenario dumped

    def test_executes_manager_rec_when_live(self):
        # An idle (REVIEW) thread whose rec has a MANAGER half: a live loop
        # (dry_run off + execute_enabled on) spawns the executor `claude -p`.
        sessions = [dict(id="cse_b", status="active", worker_status="idle",
                         connection_status="connected", environment_kind="bridge",
                         last_event_at=_ts(10_000))]
        cfg = _cfg(REMOTE_CONTROL_DEV=self._tmp.name,
                   REMOTE_CONTROL_LOGDIR=os.path.join(self._tmp.name, "logs"),
                   MANAGER_GUIDELINES_FILE=os.path.join(self._tmp.name, "none.md"),
                   MANAGER_ALLOWLIST_FILE="/nonexistent",
                   MANAGER_DRY_RUN="0", MANAGER_EXECUTE_ENABLED="1")
        advise = "ANALYSIS: done. it is.\nMANAGER: archive the finished session\nSESSION: none"
        calls = []

        def runner(cmd, cwd=None, **k):
            calls.append(cmd[-1])
            return _proc(stdout="closed it" if "EXECUTOR" in cmd[-1] else advise)

        with mock.patch("remote_control.usage_limit.monitor.list_sessions",
                        return_value=sessions), \
             mock.patch("remote_control.manager.build_worktree_index",
                        return_value={"cse_b": "myrepo"}):
            recs = monitor_tick(cfg, None, "tok", now=NOW, log=lambda m: None,
                                consider_all=True, runner=runner,
                                api=lambda *a, **k: (200, {}))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["rec_manager"], "archive the finished session")
        self.assertTrue(recs[0]["executed"])               # the executor ran
        self.assertEqual(len(calls), 2)                    # investigator + executor
        self.assertIn("archive the finished session", calls[1])   # executor carried the rec

    def test_dry_run_does_not_execute_manager_rec(self):
        # Same situation, but the default dry-run loop only logs the would-be action.
        sessions = [dict(id="cse_b", status="active", worker_status="idle",
                         connection_status="connected", environment_kind="bridge",
                         last_event_at=_ts(10_000))]
        advise = "ANALYSIS: done.\nMANAGER: archive the finished session\nSESSION: none"
        calls = []

        def runner(cmd, cwd=None, **k):
            calls.append(cmd[-1])
            return _proc(stdout=advise)

        with mock.patch("remote_control.usage_limit.monitor.list_sessions",
                        return_value=sessions), \
             mock.patch("remote_control.manager.build_worktree_index",
                        return_value={"cse_b": "myrepo"}):
            recs = monitor_tick(self.cfg, None, "tok", now=NOW, log=lambda m: None,
                                consider_all=True, runner=runner,
                                api=lambda *a, **k: (200, {}))
        self.assertFalse(recs[0]["executed"])              # not run
        self.assertEqual(len(calls), 1)                    # only the investigator spawned

    def test_empty_when_no_sessions(self):
        with mock.patch("remote_control.usage_limit.monitor.list_sessions",
                        return_value=None):
            self.assertEqual(monitor_tick(self.cfg, None, "tok", now=NOW,
                                          log=lambda m: None), [])


class DoReplayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, obj):
        p = os.path.join(self._tmp.name, "s.json")
        with open(p, "w") as fh:
            json.dump(obj, fh)
        return p

    def test_replays_and_returns_zero(self):
        p = self._write({"tool_name": "AskUserQuestion", "tool_use_id": "t",
                         "questions": [{"header": "H", "question": "Q",
                                        "options": [{"label": "A", "description": "a"}]}]})
        rc = do_replay(p, cfg=_cfg(), repo=None, log=lambda m: None,
                       runner=lambda *a, **k: _proc(stdout="Q1: A"))
        self.assertEqual(rc, 0)

    def test_no_questions_is_error(self):
        p = self._write({"questions": []})
        self.assertEqual(do_replay(p, cfg=_cfg(), repo=None, log=lambda m: None), 2)

    def test_bad_path_is_error(self):
        self.assertEqual(do_replay("/no/such/scenario.json", cfg=_cfg(), repo=None,
                                   log=lambda m: None), 2)


class FeedbackInjectionTest(unittest.TestCase):
    def _cfg_fb(self, d, entries):
        cfg = _cfg(REMOTE_CONTROL_DEV=d, REMOTE_CONTROL_LOGDIR=os.path.join(d, "logs"),
                   MANAGER_GUIDELINES_FILE=os.path.join(d, "g.md"))
        os.makedirs(cfg.logdir, exist_ok=True)
        with open(cfg.feedback_file, "w") as fh:
            json.dump(entries, fh)
        return cfg

    def test_feedback_for_prompt_own_and_same_case(self):
        with tempfile.TemporaryDirectory() as d:
            a = Action("cse_1", "r", REVIEW, "idle")
            cfg = self._cfg_fb(d, {
                action_sig(a): {"feedback": "close it", "at": "2026-01-02"},
                "review:cse_2:": {"feedback": "other review note", "at": "2026-01-03"},
                "answer:cse_3:x": {"feedback": "answer note", "at": "2026-01-04"},
            })
            out = feedback_for_prompt(cfg, a)
            self.assertIn("close it", out)             # own thread
            self.assertIn("other review note", out)    # same case (review)
            self.assertNotIn("answer note", out)       # different case excluded

    def test_feedback_injected_into_prompt(self):
        p = investigator_prompt_advise(Action("cse_1", "r", REVIEW, "idle"),
                                       feedback="- (this exact thread) do X")
        self.assertIn("PRIOR OPERATOR FEEDBACK", p)
        self.assertIn("do X", p)

    def test_analyze_action_passes_feedback_to_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "r"))
            a = Action("cse_1", "r", REVIEW, "idle", run_dir=os.path.join(d, "r"))
            cfg = self._cfg_fb(d, {action_sig(a): {"feedback": "WRITE NOTES FIRST", "at": "t"}})
            seen = {}

            def runner(cmd, **k):
                seen["prompt"] = cmd[-1]
                return _proc(stdout="RECOMMENDATION: r\nANALYSIS: a.")

            analyze_action(a, cfg=cfg, guidelines="", log=lambda m: None, runner=runner)
            self.assertIn("WRITE NOTES FIRST", seen["prompt"])

    def test_suggest_guidelines_no_feedback(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(suggest_guidelines(self._cfg_fb(d, {}), log=lambda m: None)["ok"])

    def test_suggest_guidelines_returns_proposed_doc(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg_fb(d, {"review:cse_1:": {"feedback": "close idle threads", "at": "t"}})
            with open(cfg.guidelines_file, "w") as fh:
                fh.write("# guidelines\n")
            res = suggest_guidelines(cfg, log=lambda m: None,
                                     runner=lambda *a, **k: _proc(
                                         stdout="# revised\nGUIDELINE: close idle threads"))
            self.assertTrue(res["ok"])
            self.assertIn("revised", res["text"])


class EnrichTest(unittest.TestCase):
    """title / worktree status / last messages shown per row in the ui."""
    def test_title_on_action_and_row(self):
        from remote_control.manager import action_row
        s = _sess(title="[AH] my session", worker_status="idle", last_event_at=_ts(9999))
        a = _plan(s)
        self.assertEqual(a.title, "[AH] my session")
        self.assertEqual(action_row(a)["title"], "[AH] my session")

    def test_worktree_for_finds_bridge_dir(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(REMOTE_CONTROL_DEV=d)
            wt = os.path.join(d, "ai-harness", ".claude", "worktrees", "bridge-cse_z")
            os.makedirs(wt)
            self.assertEqual(str(worktree_for(cfg, "cse_z")), wt)
            self.assertIsNone(worktree_for(cfg, "cse_missing"))

    def test_worktree_status_missing(self):
        self.assertEqual(worktree_status(None), {"exists": False})
        self.assertEqual(worktree_status("/no/such/dir"), {"exists": False})

    def test_worktree_status_real_git_dir(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "-C", d, "init", "-q"], check=True)
            with open(os.path.join(d, "f.txt"), "w") as fh:
                fh.write("x")                      # untracked -> dirty
            st = worktree_status(d)
            self.assertTrue(st["exists"])
            self.assertGreaterEqual(st["dirty"], 1)

    def test_last_messages_returns_last_n_text_turns(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            with open(p, "w") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": "first?"}}) + "\n")
                fh.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "an answer"}]}}) + "\n")
                fh.write(json.dumps({"type": "user", "isMeta": True,
                                     "message": {"content": "<meta>"}}) + "\n")
                fh.write(json.dumps({"type": "user", "message": {"content": "second?"}}) + "\n")
            msgs = last_messages(p, n=2)
            self.assertEqual([m["role"] for m in msgs], ["assistant", "user"])
            self.assertEqual(msgs[-1]["text"], "second?")

    def test_last_messages_no_path(self):
        self.assertEqual(last_messages(None), [])

    def test_session_transcript_maps_via_worktree_slug(self):
        import re as _re
        from pathlib import Path as _P
        from remote_control.manager import session_transcript
        with tempfile.TemporaryDirectory() as dev, tempfile.TemporaryDirectory() as home:
            cfg = _cfg(REMOTE_CONTROL_DEV=dev)
            wt = os.path.join(dev, "ai-harness", ".claude", "worktrees", "bridge-cse_t")
            os.makedirs(wt)
            proj = os.path.join(home, ".claude", "projects",
                                _re.sub(r"[^A-Za-z0-9]", "-", wt))
            os.makedirs(proj)
            tp = os.path.join(proj, "uuid.jsonl")
            with open(tp, "w") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": "hi there"}}) + "\n")
            with mock.patch("remote_control.manager.Path.home", return_value=_P(home)):
                found = session_transcript(cfg, "cse_t")
            self.assertEqual(str(found), tp)
            self.assertEqual(last_messages(found)[-1]["text"], "hi there")

    def test_session_mtime_reflects_transcript(self):
        from remote_control.manager import session_mtime
        with tempfile.TemporaryDirectory() as dev:
            cfg = _cfg(REMOTE_CONTROL_DEV=dev)
            # no local worktree/transcript -> unknown
            self.assertIsNone(session_mtime(cfg, "cse_missing"))
            tp = os.path.join(dev, "t.jsonl")
            with open(tp, "w") as fh:
                fh.write("{}\n")
            os.utime(tp, (1_000_000_000, 1_000_000_000))
            with mock.patch("remote_control.manager.session_transcript",
                            return_value=__import__("pathlib").Path(tp)):
                self.assertEqual(session_mtime(cfg, "cse_t"), 1_000_000_000.0)


class AnalysisStaleTest(unittest.TestCase):
    """The pure 'has the session moved past this analysis?' predicate."""
    AT = NOW.isoformat(timespec="seconds")     # what decision_record stores in "ts"

    def test_session_updated_after_analysis_is_stale(self):
        self.assertTrue(analysis_is_stale(self.AT, NOW_EPOCH + 100))

    def test_session_updated_before_analysis_is_fresh(self):
        self.assertFalse(analysis_is_stale(self.AT, NOW_EPOCH - 100))

    def test_within_slack_is_not_stale(self):
        # a write a second after the (floored) analysis ts is treated as concurrent
        self.assertFalse(analysis_is_stale(self.AT, NOW_EPOCH + 1))
        self.assertTrue(analysis_is_stale(self.AT, NOW_EPOCH + 100, slack_secs=0))

    def test_unknown_inputs_never_stale(self):
        self.assertFalse(analysis_is_stale(self.AT, None))     # no local transcript
        self.assertFalse(analysis_is_stale("", NOW_EPOCH + 100))   # never analyzed
        self.assertFalse(analysis_is_stale("not-a-date", NOW_EPOCH + 100))


if __name__ == "__main__":
    unittest.main()
