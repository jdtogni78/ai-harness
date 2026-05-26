import unittest

from remote_control.session_list import (
    build_rows,
    classify_location,
    format_rows,
    is_active,
    is_stale,
    parse_duration,
    summarize,
)


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


if __name__ == "__main__":
    unittest.main()
