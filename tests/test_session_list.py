import base64
import contextlib
import io
import json
import unittest
from unittest import mock

from remote_control.session_list import (
    _run_submit,
    build_rows,
    classify_location,
    format_reply_header,
    format_rows,
    is_active,
    is_stale,
    own_session_id_from_env,
    parse_duration,
    summarize,
)
from remote_control.usage_limit import monitor as monitor_mod
from remote_control.usage_limit.detect import resume_event_body


def _session(**kw) -> dict:
    base = {
        "id": "cse_x",
        "title": "t",
        "status": "active",
        "environment_kind": "bridge",
        "worker_status": "idle",
        "connection_status": "connected",
        "last_event_at": "2026-05-24T00:00:00Z",
        "config": {"sources": []},
    }
    base.update(kw)
    return base


class IsActiveTest(unittest.TestCase):
    def test_archived_is_inactive(self):
        self.assertFalse(is_active(_session(status="archived")))

    def test_active_is_active(self):
        self.assertTrue(is_active(_session(status="active")))

    def test_unknown_status_counts_as_active(self):
        # "not archived" -> any other/missing status still surfaces.
        self.assertTrue(is_active({"id": "cse_x"}))


class ClassifyLocationTest(unittest.TestCase):
    def test_cloud_kind(self):
        s = _session(id="cse_c", environment_kind="anthropic_cloud")
        self.assertEqual(classify_location(s, {}), "cloud")

    def test_bridge_with_local_worktree_is_this_host(self):
        s = _session(id="cse_b")
        self.assertEqual(classify_location(s, {"cse_b": "AppOne"}), "this-host")

    def test_bridge_without_local_worktree_is_other_host(self):
        s = _session(id="cse_b")
        self.assertEqual(classify_location(s, {}), "other-host")

    def test_cloud_takes_precedence_over_index(self):
        # A cloud session never counts as local even if its id is somehow indexed.
        s = _session(id="cse_c", environment_kind="anthropic_cloud")
        self.assertEqual(classify_location(s, {"cse_c": "AppOne"}), "cloud")


class BuildRowsTest(unittest.TestCase):
    def setUp(self):
        self.index = {"cse_local": "ai-harness"}
        self.sessions = [
            _session(id="cse_local", title="here",
                     last_event_at="2026-05-24T03:00:00Z"),
            _session(id="cse_cloud", title="in cloud",
                     environment_kind="anthropic_cloud",
                     last_event_at="2026-05-24T05:00:00Z",
                     config={"sources": [
                         {"url": "https://github.com/me/AppOne.git"}]}),
            _session(id="cse_archived", title="old", status="archived",
                     last_event_at="2026-05-24T09:00:00Z"),
        ]

    def test_excludes_archived_by_default(self):
        ids = [r.id for r in build_rows(self.sessions, self.index)]
        self.assertEqual(ids, ["cse_cloud", "cse_local"])  # newest first

    def test_include_archived(self):
        ids = {r.id for r in build_rows(self.sessions, self.index,
                                        include_archived=True)}
        self.assertEqual(ids, {"cse_local", "cse_cloud", "cse_archived"})

    def test_sorted_newest_first(self):
        rows = build_rows(self.sessions, self.index, include_archived=True)
        self.assertEqual([r.id for r in rows],
                         ["cse_archived", "cse_cloud", "cse_local"])

    def test_repo_and_location_resolved(self):
        rows = {r.id: r for r in build_rows(self.sessions, self.index)}
        self.assertEqual(rows["cse_local"].repo, "ai-harness")
        self.assertEqual(rows["cse_local"].location, "this-host")
        self.assertEqual(rows["cse_cloud"].repo, "AppOne")  # from sources url
        self.assertEqual(rows["cse_cloud"].location, "cloud")

    def test_repo_filter_is_case_insensitive(self):
        rows = build_rows(self.sessions, self.index, repo_filter="appone")
        self.assertEqual([r.id for r in rows], ["cse_cloud"])

    def test_unknown_repo_is_none(self):
        s = [_session(id="cse_orphan", config={"sources": []})]
        self.assertIsNone(build_rows(s, {})[0].repo)


class FormatTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_rows([], "host"), "(no sessions)")

    def test_table_names_local_host_and_unknown_repo(self):
        rows = build_rows([_session(id="cse_o", config={"sources": []})], {})
        text = format_rows(rows, "mymac")
        self.assertIn("cse_o", text)
        self.assertIn("<unknown>", text)        # repo unresolved
        self.assertIn("other host", text)       # not in local index

    def test_table_labels_this_host_with_name(self):
        rows = build_rows([_session(id="cse_l")], {"cse_l": "ai-harness"})
        self.assertIn("this host (mymac)", format_rows(rows, "mymac"))

    def test_summarize_tallies_repo_and_host(self):
        rows = build_rows([
            _session(id="cse_l"),
        ], {"cse_l": "ai-harness"})
        out = summarize(rows)
        self.assertIn("ai-harness×1", out)
        self.assertIn("this-host×1", out)


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("30"), 30)
        self.assertEqual(parse_duration("30s"), 30)
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration(" 5 m "), 300)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_duration("forever")
        with self.assertRaises(ValueError):
            parse_duration("5y")


# Fixed reference moment + the epoch 1h later. Derived (not hardcoded) so the
# tests can't drift from the same datetime semantics is_stale uses.
from datetime import datetime, timezone as _tz
_T = "2026-05-26T04:00:00Z"
_NOW = datetime(2026, 5, 26, 5, 0, 0, tzinfo=_tz.utc).timestamp()


class IsStaleTest(unittest.TestCase):
    def test_idle_old_enough_is_stale(self):
        s = _session(worker_status="idle", last_event_at=_T)
        self.assertTrue(is_stale(s, _NOW, 1800))   # 1h old, threshold 30m

    def test_running_is_never_stale(self):
        s = _session(worker_status="running", last_event_at=_T)
        self.assertFalse(is_stale(s, _NOW, 60))

    def test_too_recent_is_not_stale(self):
        s = _session(worker_status="idle", last_event_at=_T)
        self.assertFalse(is_stale(s, _NOW, 7200))  # 1h old, threshold 2h

    def test_require_disconnected_filters_connected(self):
        s = _session(worker_status="idle", connection_status="connected",
                     last_event_at=_T)
        self.assertFalse(is_stale(s, _NOW, 60, require_disconnected=True))
        s2 = _session(worker_status="idle", connection_status="disconnected",
                      last_event_at=_T)
        self.assertTrue(is_stale(s2, _NOW, 60, require_disconnected=True))

    def test_unparseable_timestamp_is_not_stale(self):
        s = _session(worker_status="idle", last_event_at="not-a-date")
        self.assertFalse(is_stale(s, _NOW, 60))


class StaleAndLocationFilterTest(unittest.TestCase):
    def setUp(self):
        # Two stale this-host, one fresh this-host, one stale other-host.
        self.sessions = [
            _session(id="cse_stale_local_a", worker_status="idle",
                     connection_status="disconnected", last_event_at=_T),
            _session(id="cse_stale_local_b", worker_status="idle",
                     connection_status="disconnected", last_event_at=_T),
            _session(id="cse_fresh_local", worker_status="running",
                     last_event_at=_T),
            _session(id="cse_stale_other", worker_status="idle",
                     connection_status="disconnected", last_event_at=_T),
        ]
        self.index = {"cse_stale_local_a": "ai-harness",
                      "cse_stale_local_b": "ai-harness",
                      "cse_fresh_local": "ai-harness"}

    def test_stale_only(self):
        rows = build_rows(self.sessions, self.index,
                          stale_only=True, stale_age_secs=60, now=_NOW)
        self.assertEqual({r.id for r in rows},
                         {"cse_stale_local_a", "cse_stale_local_b", "cse_stale_other"})

    def test_stale_plus_this_host(self):
        rows = build_rows(self.sessions, self.index,
                          stale_only=True, stale_age_secs=60,
                          location_filter="this-host", now=_NOW)
        self.assertEqual({r.id for r in rows},
                         {"cse_stale_local_a", "cse_stale_local_b"})

    def test_stale_plus_disconnected_excludes_connected_idle(self):
        s = _session(id="cse_connected_idle", worker_status="idle",
                     connection_status="connected", last_event_at=_T)
        rows = build_rows([s] + self.sessions, self.index,
                          stale_only=True, stale_age_secs=60,
                          stale_require_disconnected=True, now=_NOW)
        self.assertNotIn("cse_connected_idle", {r.id for r in rows})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fake_jwt(payload: dict) -> str:
    header = _b64url(b'{"typ":"JWT"}')
    body = _b64url(json.dumps(payload).encode("utf-8"))
    sig = _b64url(b"sig")
    return f"{header}.{body}.{sig}"


class OwnSessionIdFromEnvTest(unittest.TestCase):
    def test_extracts_cse_id_from_jwt_token(self):
        jwt = _fake_jwt({"session_id": "cse_01ABC"})
        env = {"CLAUDE_CODE_SESSION_ACCESS_TOKEN": f"sk-ant-si-{jwt}"}
        self.assertEqual(own_session_id_from_env(env), "cse_01ABC")

    def test_missing_env_var_returns_none(self):
        self.assertIsNone(own_session_id_from_env({}))

    def test_empty_env_var_returns_none(self):
        self.assertIsNone(own_session_id_from_env({"CLAUDE_CODE_SESSION_ACCESS_TOKEN": ""}))

    def test_malformed_token_returns_none(self):
        env = {"CLAUDE_CODE_SESSION_ACCESS_TOKEN": "sk-ant-si-not-a-jwt"}
        self.assertIsNone(own_session_id_from_env(env))

    def test_non_cse_session_id_is_ignored(self):
        # parse_cmd_session_id only accepts ids that start with "cse_".
        jwt = _fake_jwt({"session_id": "local-uuid-abc"})
        env = {"CLAUDE_CODE_SESSION_ACCESS_TOKEN": f"sk-ant-si-{jwt}"}
        self.assertIsNone(own_session_id_from_env(env))


class FormatReplyHeaderTest(unittest.TestCase):
    def test_prepends_single_line_header(self):
        out = format_reply_header("cse_01XYZ", "continue")
        self.assertTrue(out.startswith("[from cse_01XYZ — reply via send-to-session]"))
        self.assertIn("\n\ncontinue", out)

    def test_preserves_original_body_verbatim(self):
        body = "line one\nline two\n"
        out = format_reply_header("cse_01XYZ", body)
        self.assertTrue(out.endswith(body))


def _silent_log(*_a, **_kw):
    pass


@contextlib.contextmanager
def _capture():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class RunSubmitArgsTest(unittest.TestCase):
    """``sessions submit`` arg parsing -- the rc=2 / usage paths that must reject
    bad invocations *before* any network or keychain call. None of these should
    touch :func:`monitor.api_request`."""

    def test_missing_cse_id_returns_2(self):
        with _capture() as (_out, err):
            rc = _run_submit(["--message", "hi", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 2)
        self.assertIn("submit requires a CSE_ID", err.getvalue())

    def test_message_and_stdin_mutually_exclusive(self):
        with _capture() as (_out, err):
            rc = _run_submit(
                ["cse_x", "--message", "hi", "--stdin", "--no-reply-to"],
                _silent_log)
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", err.getvalue())

    def test_missing_message_returns_2(self):
        with _capture() as (_out, err):
            rc = _run_submit(["cse_x", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 2)
        self.assertIn("--message TEXT or --stdin", err.getvalue())

    def test_empty_message_returns_2(self):
        with _capture() as (_out, err):
            rc = _run_submit(
                ["cse_x", "--message", "", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 2)
        self.assertIn("message is empty", err.getvalue())

    def test_whitespace_only_message_returns_2(self):
        # "not empty" must mean has-content, not has-bytes -- "   \n" must reject.
        with _capture() as (_out, err):
            rc = _run_submit(
                ["cse_x", "--message", "   \n", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 2)
        self.assertIn("message is empty", err.getvalue())


class RunSubmitDryRunTest(unittest.TestCase):
    """``--dry-run`` must short-circuit *before* the keychain + network. The
    printed JSON must be exactly :func:`resume_event_body`'s shape so the
    user can see what a real POST would send."""

    def setUp(self):
        # Fake cfg with just enough surface area for the dry-run print path.
        self.fake_cfg = mock.Mock()
        self.fake_cfg.api_base = "https://api.test"
        self._cfg_patch = mock.patch(
            "remote_control.session_list.UsageLimitConfig.from_env",
            return_value=self.fake_cfg)
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()

    def test_dry_run_message_prints_body_and_never_calls_api_request(self):
        with mock.patch("remote_control.session_list.monitor") as monitor, \
                _capture() as (out, _err):
            rc = _run_submit(
                ["cse_x", "--message", "hello", "--dry-run", "--no-reply-to"],
                _silent_log)
        self.assertEqual(rc, 0)
        monitor.api_request.assert_not_called()
        monitor.submit_user_message.assert_not_called()
        monitor.get_token.assert_not_called()
        # The printed JSON is exactly the wrapped-event shape.
        text = out.getvalue()
        self.assertIn("would POST https://api.test/sessions/cse_x/events", text)
        body_json = text.split("\n", 1)[1]
        self.assertEqual(json.loads(body_json), resume_event_body("hello"))

    def test_dry_run_stdin_reads_stdin_and_prints_body(self):
        # --stdin reads sys.stdin; patch it for determinism.
        with mock.patch("remote_control.session_list.monitor") as monitor, \
                mock.patch("sys.stdin", io.StringIO("from-stdin")), \
                _capture() as (out, _err):
            rc = _run_submit(
                ["cse_x", "--stdin", "--dry-run", "--no-reply-to"],
                _silent_log)
        self.assertEqual(rc, 0)
        monitor.api_request.assert_not_called()
        text = out.getvalue()
        self.assertIn("would POST https://api.test/sessions/cse_x/events", text)
        body_json = text.split("\n", 1)[1]
        self.assertEqual(json.loads(body_json), resume_event_body("from-stdin"))


class RunSubmitNetworkTest(unittest.TestCase):
    """Non-dry-run paths through ``_run_submit`` -- the CLI delegates to
    :func:`monitor.submit_user_message`, and the rc derives from its http code."""

    def setUp(self):
        self._cfg_patch = mock.patch(
            "remote_control.session_list.UsageLimitConfig.from_env",
            return_value=mock.Mock(api_base="https://api.test"))
        self._cfg_patch.start()

    def tearDown(self):
        self._cfg_patch.stop()

    def test_happy_path_returns_0_and_calls_submit(self):
        with mock.patch("remote_control.session_list.monitor") as monitor, \
                _capture() as (out, _err):
            monitor.get_token.return_value = "tok"
            monitor.submit_user_message.return_value = (200, {"ok": True})
            rc = _run_submit(
                ["cse_x", "--message", "hi", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 0)
        # Called once with (cfg, token, sid, message, log).
        monitor.submit_user_message.assert_called_once()
        args, _ = monitor.submit_user_message.call_args
        self.assertEqual(args[1], "tok")
        self.assertEqual(args[2], "cse_x")
        self.assertEqual(args[3], "hi")
        self.assertIn("submitted cse_x", out.getvalue())

    def test_non_200_returns_1_and_logs_to_stderr(self):
        with mock.patch("remote_control.session_list.monitor") as monitor, \
                _capture() as (_out, err):
            monitor.get_token.return_value = "tok"
            monitor.submit_user_message.return_value = (503, "down")
            rc = _run_submit(
                ["cse_x", "--message", "hi", "--no-reply-to"], _silent_log)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED cse_x", err.getvalue())
        self.assertIn("503", err.getvalue())

    def test_missing_token_returns_1(self):
        with mock.patch("remote_control.session_list.monitor") as monitor:
            monitor.get_token.return_value = None
            with _capture():
                rc = _run_submit(
                    ["cse_x", "--message", "hi", "--no-reply-to"], _silent_log)
            self.assertEqual(rc, 1)
            monitor.submit_user_message.assert_not_called()

    def test_reply_to_prepends_header_to_submitted_message(self):
        # Explicit --reply-to should embed the [from <sender>] header in the
        # message that monitor.submit_user_message ultimately receives.
        with mock.patch("remote_control.session_list.monitor") as monitor:
            monitor.get_token.return_value = "tok"
            monitor.submit_user_message.return_value = (200, {})
            with _capture():
                rc = _run_submit(
                    ["cse_x", "--message", "hi", "--reply-to", "cse_sender"],
                    _silent_log)
            self.assertEqual(rc, 0)
            sent = monitor.submit_user_message.call_args.args[3]
            self.assertTrue(sent.startswith(
                "[from cse_sender — reply via send-to-session]"))
            self.assertIn("hi", sent)


class SubmitUserMessageTest(unittest.TestCase):
    """``monitor.submit_user_message`` is a thin wrapper over
    :func:`monitor.api_request`; the test patches ``api_request`` so nothing
    hits the network. Same approach the archive tests use."""

    def test_happy_path_posts_events_with_wrapped_body(self):
        cfg = mock.Mock()
        with mock.patch.object(monitor_mod, "api_request",
                               return_value=(200, {"id": "evt_1"})) as api:
            code, body = monitor_mod.submit_user_message(
                cfg, "tok", "cse_x", "hi", _silent_log)
        self.assertEqual(code, 200)
        self.assertEqual(body, {"id": "evt_1"})
        api.assert_called_once_with(
            cfg, "POST", "/sessions/cse_x/events", "tok",
            resume_event_body("hi"))

    def test_non_200_returns_code_and_logs(self):
        cfg = mock.Mock()
        logs = []
        with mock.patch.object(monitor_mod, "api_request",
                               return_value=(503, "down")):
            code, body = monitor_mod.submit_user_message(
                cfg, "tok", "cse_x", "hi", logs.append)
        self.assertEqual(code, 503)
        self.assertEqual(body, "down")
        self.assertTrue(any("submit cse_x failed" in m and "503" in m
                            for m in logs),
                        f"expected a diagnostic log line, got: {logs!r}")


class ResumeEventBodyShapeTest(unittest.TestCase):
    """The wrapped-event body the submit CLI ships is the same shape
    ``attempt_resume`` POSTs -- if these drift, the submit path silently
    starts sending an unrecognized shape. Pin the shape with a literal."""

    def test_shape_matches_verified_live_attempt_resume_body(self):
        self.assertEqual(resume_event_body("x"), {
            "events": [{
                "event_type": "user",
                "source": "client",
                "payload": {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "x"}],
                    },
                },
            }],
        })


if __name__ == "__main__":
    unittest.main()
