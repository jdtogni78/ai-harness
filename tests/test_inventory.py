import unittest
from datetime import datetime, timezone

from remote_control.inventory import (
    WorkRow,
    activity_of,
    claude_rows_to_work,
    codex_sessions_to_work,
    format_work_rows,
    merge_rows,
    parse_ts,
    repo_from_cwd,
    stale_reason,
    summarize_work,
)
from remote_control.session_list import Row
from remote_control.session_port.codex import CodexSession

NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


class ParseTsTest(unittest.TestCase):
    def test_zulu_suffix(self):
        self.assertEqual(parse_ts("2026-05-24T12:00:00Z"),
                         datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc))

    def test_offset_suffix(self):
        self.assertEqual(parse_ts("2026-05-24T12:00:00+00:00"),
                         datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc))

    def test_naive_assumed_utc(self):
        self.assertEqual(parse_ts("2026-05-24T12:00:00").tzinfo, timezone.utc)

    def test_empty_and_garbage_are_none(self):
        self.assertIsNone(parse_ts(""))
        self.assertIsNone(parse_ts("not-a-date"))


class ActivityOfTest(unittest.TestCase):
    def test_recent_is_active(self):
        self.assertEqual(activity_of("2026-05-24T11:30:00Z", NOW, 3600), "active")

    def test_old_is_idle(self):
        self.assertEqual(activity_of("2026-05-24T10:00:00Z", NOW, 3600), "idle")

    def test_boundary_is_idle(self):  # exactly ttl away -> not < ttl
        self.assertEqual(activity_of("2026-05-24T11:00:00Z", NOW, 3600), "idle")

    def test_unknown_is_idle(self):
        self.assertEqual(activity_of("", NOW, 3600), "idle")

    def test_future_timestamp_is_idle(self):  # negative delta excluded
        self.assertEqual(activity_of("2026-05-24T13:00:00Z", NOW, 3600), "idle")


class RepoFromCwdTest(unittest.TestCase):
    DEV = "/Users/x/dev"

    def test_repo_root(self):
        self.assertEqual(repo_from_cwd("/Users/x/dev/AppOne", self.DEV), "AppOne")

    def test_worktree_path_yields_repo(self):
        cwd = "/Users/x/dev/ai-harness/.claude/worktrees/bridge-cse_01AB"
        self.assertEqual(repo_from_cwd(cwd, self.DEV), "ai-harness")

    def test_outside_dev_is_none(self):
        self.assertIsNone(repo_from_cwd("/tmp/scratch", self.DEV))

    def test_dev_root_itself_is_none(self):
        self.assertIsNone(repo_from_cwd(self.DEV, self.DEV))

    def test_empty_is_none(self):
        self.assertIsNone(repo_from_cwd("", self.DEV))


def _crow(**kw) -> Row:
    base = dict(id="cse_x", title="t", repo="ai-harness", env_kind="bridge",
                location="this-host", worker_status="idle",
                connection_status="connected", last_event_at="2026-05-24T11:45:00Z")
    base.update(kw)
    return Row(**base)


class ClaudeAdapterTest(unittest.TestCase):
    def test_maps_fields_and_recent_activity(self):
        [w] = claude_rows_to_work([_crow()], NOW, 3600)
        self.assertEqual(w.engine, "claude")
        self.assertEqual(w.repo, "ai-harness")
        self.assertEqual(w.location, "this-host")
        self.assertEqual(w.activity, "active")
        self.assertEqual(w.detail, "worker=idle conn=connected")

    def test_old_session_is_idle(self):
        [w] = claude_rows_to_work([_crow(last_event_at="2026-05-20T00:00:00Z")], NOW, 3600)
        self.assertEqual(w.activity, "idle")


def _cx(**kw) -> CodexSession:
    base = dict(id="019e-aaa", title="codex thread", cwd="/Users/x/dev/AppOne",
                path="/p", updated_at="2026-05-24T11:45:00Z", archived=False)
    base.update(kw)
    return CodexSession(**base)


class CodexAdapterTest(unittest.TestCase):
    DEV = "/Users/x/dev"

    def test_maps_fields_repo_and_location(self):
        [w] = codex_sessions_to_work([_cx()], self.DEV, NOW, 3600)
        self.assertEqual(w.engine, "codex")
        self.assertEqual(w.repo, "AppOne")
        self.assertEqual(w.location, "this-host")
        self.assertEqual(w.activity, "active")
        self.assertEqual(w.detail, "")

    def test_archived_detail(self):
        [w] = codex_sessions_to_work([_cx(archived=True)], self.DEV, NOW, 3600)
        self.assertEqual(w.detail, "archived")

    def test_ts_of_injection_overrides_updated_at(self):
        # index-less session: ts_of supplies a (fresh) timestamp.
        [w] = codex_sessions_to_work(
            [_cx(updated_at="")], self.DEV, NOW, 3600,
            ts_of=lambda s: "2026-05-24T11:59:00Z")
        self.assertEqual(w.last_event_at, "2026-05-24T11:59:00Z")
        self.assertEqual(w.activity, "active")

    def test_unknown_ts_is_idle(self):
        [w] = codex_sessions_to_work([_cx(updated_at="")], self.DEV, NOW, 3600)
        self.assertEqual(w.activity, "idle")
        self.assertEqual(w.last_event_at, "")

    def test_signals_only_consulted_for_idle(self):
        calls = []

        def signals(s):
            calls.append(s.id)
            return {"awaiting": True}

        # active session -> signals_of skipped, never stale
        [w] = codex_sessions_to_work([_cx(id="fresh")], self.DEV, NOW, 3600,
                                     signals_of=signals)
        self.assertEqual(calls, [])
        self.assertEqual(w.stale_reason, "")

    def test_idle_awaiting_is_stale(self):
        [w] = codex_sessions_to_work(
            [_cx(id="old", updated_at="2026-05-20T00:00:00Z")], self.DEV, NOW, 3600,
            signals_of=lambda s: {"awaiting": True})
        self.assertEqual(w.activity, "idle")
        self.assertEqual(w.stale_reason, "awaiting response")


class StaleReasonTest(unittest.TestCase):
    def test_active_never_stale(self):
        self.assertEqual(stale_reason(active=True, limit_paused=True,
                                      disconnected=True, awaiting=True), "")

    def test_idle_with_nothing_pending_not_stale(self):
        self.assertEqual(stale_reason(active=False), "")

    def test_limit_paused_wins(self):
        self.assertEqual(
            stale_reason(active=False, limit_paused=True, disconnected=True,
                         awaiting=True),
            "usage-limit paused")

    def test_disconnected(self):
        self.assertEqual(stale_reason(active=False, disconnected=True),
                         "disconnected (server gone)")

    def test_awaiting(self):
        self.assertEqual(stale_reason(active=False, awaiting=True), "awaiting response")

    def test_running_idle_not_flagged(self):
        # a quiet "running" worker is indistinguishable from a long tool call,
        # so it is NOT called stale (avoids false positives).
        rows = [_crow(worker_status="running", connection_status="connected",
                      last_event_at="2026-05-20T00:00:00Z")]
        [w] = claude_rows_to_work(rows, NOW, 3600)
        self.assertEqual(w.stale_reason, "")


class ClaudeStaleTest(unittest.TestCase):
    def test_limit_paused_id(self):
        rows = [_crow(id="cse_p", worker_status="idle",
                      last_event_at="2026-05-20T00:00:00Z")]
        [w] = claude_rows_to_work(rows, NOW, 3600, limit_paused_ids={"cse_p"})
        self.assertEqual(w.stale_reason, "usage-limit paused")

    def test_requires_action_idle_is_awaiting(self):
        rows = [_crow(worker_status="requires_action",
                      last_event_at="2026-05-20T00:00:00Z")]
        [w] = claude_rows_to_work(rows, NOW, 3600)
        self.assertEqual(w.stale_reason, "awaiting response")

    def test_disconnected_idle(self):
        rows = [_crow(connection_status="disconnected",
                      last_event_at="2026-05-20T00:00:00Z")]
        [w] = claude_rows_to_work(rows, NOW, 3600)
        self.assertEqual(w.stale_reason, "disconnected (server gone)")

    def test_recent_requires_action_not_stale(self):
        # active (recent) work is never stale, even mid-action
        rows = [_crow(worker_status="requires_action")]
        [w] = claude_rows_to_work(rows, NOW, 3600)
        self.assertEqual(w.stale_reason, "")


def _w(engine="claude", repo="ai-harness", activity="active",
       last="2026-05-24T11:45:00Z", id="i", stale="") -> WorkRow:
    return WorkRow(engine=engine, id=id, title="t", repo=repo, location="this-host",
                   activity=activity, detail="", last_event_at=last, stale_reason=stale)


class MergeRowsTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _w(engine="claude", repo="ai-harness", id="c1", last="2026-05-24T11:00:00Z"),
            _w(engine="codex", repo="AppOne", id="x1", last="2026-05-24T11:50:00Z"),
            _w(engine="codex", repo="ai-harness", activity="idle", id="x2",
               last="2026-05-24T09:00:00Z"),
        ]

    def test_sorted_newest_first(self):
        self.assertEqual([r.id for r in merge_rows(self.rows)], ["x1", "c1", "x2"])

    def test_include_idle_false_drops_idle(self):
        ids = [r.id for r in merge_rows(self.rows, include_idle=False)]
        self.assertEqual(ids, ["x1", "c1"])

    def test_engine_filter(self):
        ids = {r.id for r in merge_rows(self.rows, engine_filter="codex")}
        self.assertEqual(ids, {"x1", "x2"})

    def test_repo_filter_case_insensitive(self):
        ids = {r.id for r in merge_rows(self.rows, repo_filter="appone")}
        self.assertEqual(ids, {"x1"})

    def test_unknown_repo_excluded_by_repo_filter(self):
        rows = [_w(repo=None, id="orphan")]
        self.assertEqual(merge_rows(rows, repo_filter="ai-harness"), [])

    def test_unparseable_ts_sorts_last(self):
        rows = [_w(id="bad", last=""), _w(id="good", last="2026-05-24T11:00:00Z")]
        self.assertEqual([r.id for r in merge_rows(rows)], ["good", "bad"])


class FormatTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(format_work_rows([], "host"), "(no work)")

    def test_stanza_has_engine_repo_host_and_activity(self):
        text = format_work_rows([_w(engine="codex", id="x9")], "mini")
        self.assertIn("[codex ] x9", text)
        self.assertIn("this host (mini)", text)
        self.assertIn("[active]", text)

    def test_detail_line_omitted_when_empty(self):
        text = format_work_rows([_w()], "mini")
        self.assertNotIn("state :", text)

    def test_detail_line_present_when_set(self):
        row = _w()._replace(detail="worker=idle conn=connected")
        self.assertIn("state : worker=idle", format_work_rows([row], "mini"))

    def test_stale_line_shown_when_set(self):
        row = _w(activity="idle", stale="usage-limit paused")
        self.assertIn("stale : usage-limit paused", format_work_rows([row], "mini"))

    def test_stale_line_absent_when_unset(self):
        self.assertNotIn("stale :", format_work_rows([_w()], "mini"))

    def test_summarize_tallies_engine_repo_activity(self):
        out = summarize_work([
            _w(engine="claude", repo="ai-harness", activity="active"),
            _w(engine="codex", repo="AppOne", activity="idle"),
        ])
        self.assertIn("by engine", out)
        self.assertIn("claude×1", out)
        self.assertIn("codex×1", out)
        self.assertIn("active×1", out)

    def test_summarize_adds_stale_tally_only_when_present(self):
        no_stale = summarize_work([_w()])
        self.assertNotIn("by stale", no_stale)
        with_stale = summarize_work([
            _w(activity="idle", stale="awaiting response"),
            _w(activity="idle", stale="awaiting response", id="j"),
        ])
        self.assertIn("by stale", with_stale)
        self.assertIn("awaiting response×2", with_stale)


if __name__ == "__main__":
    unittest.main()
