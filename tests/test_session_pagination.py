"""``monitor.list_sessions`` must return ALL sessions, not the first page.

The single-page version silently hid ~90% of a 936-session account, and every
consumer reads "absent from this list" as a hard fact ("session is gone", "safe
to reap"). These tests pin the walk, the de-dup, and the fail-closed behaviour.
"""

import unittest

from remote_control import config
from remote_control.usage_limit import monitor


def sess(i):
    return {"id": f"cse_{i:0>22}", "status": "active"}


class FakeApi:
    """Records requested paths and serves pages from a session list."""

    def __init__(self, sessions, page=100, fail_on_page=None, code=200):
        self.sessions = sessions
        self.page = page
        self.fail_on_page = fail_on_page
        self.code = code
        self.paths = []

    def __call__(self, cfg, method, path, token, body=None):
        n = len(self.paths)
        self.paths.append(path)
        if self.fail_on_page is not None and n == self.fail_on_page:
            return self.code, {"error": "boom"}
        # Cursor is the index of the next item to serve.
        start = 0
        if "cursor=" in path:
            start = int(path.split("cursor=")[1].split("&")[0])
        batch = self.sessions[start:start + self.page]
        nxt = start + self.page
        return 200, {
            "data": batch,
            "next_cursor": str(nxt) if nxt < len(self.sessions) else None,
        }


class ListSessionsPaginationTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.UsageLimitConfig.from_env({"HOME": "/tmp"})
        self.logs = []
        self._orig = monitor.api_request

    def tearDown(self):
        monitor.api_request = self._orig

    def run_list(self, api):
        monitor.api_request = api
        return monitor.list_sessions(self.cfg, "tok", self.logs.append)

    def test_returns_far_more_than_one_page(self):
        # The actual regression: 936 sessions, 100-item pages.
        all_sessions = [sess(i) for i in range(936)]
        api = FakeApi(all_sessions)
        got = self.run_list(api)
        self.assertEqual(len(got), 936)
        self.assertEqual([s["id"] for s in got],
                         [s["id"] for s in all_sessions])
        self.assertEqual(len(api.paths), 10)  # 9 full pages + a partial

    def test_single_short_page_makes_exactly_one_request(self):
        api = FakeApi([sess(i) for i in range(5)])
        self.assertEqual(len(self.run_list(api)), 5)
        self.assertEqual(len(api.paths), 1)
        self.assertNotIn("cursor=", api.paths[0])

    def test_exactly_one_full_page_still_terminates(self):
        # next_cursor is None at the boundary -> no extra request, no hang.
        api = FakeApi([sess(i) for i in range(100)])
        self.assertEqual(len(self.run_list(api)), 100)
        self.assertEqual(len(api.paths), 1)

    def test_empty_account(self):
        api = FakeApi([])
        self.assertEqual(self.run_list(api), [])

    def test_duplicates_across_pages_are_collapsed(self):
        # Sessions can shift between pages mid-walk; ids must not repeat.
        dup = sess(1)
        pages = [
            (200, {"data": [dup, sess(2)], "next_cursor": "x"}),
            (200, {"data": [dup, sess(3)], "next_cursor": None}),
        ]
        it = iter(pages)
        monitor.api_request = lambda *a, **k: next(it)
        got = monitor.list_sessions(self.cfg, "tok", self.logs.append)
        self.assertEqual([s["id"] for s in got],
                         [dup["id"], sess(2)["id"], sess(3)["id"]])

    def test_non_advancing_cursor_terminates(self):
        # A server that always returns the same page + a cursor must not spin.
        monitor.api_request = lambda *a, **k: (
            200, {"data": [sess(1)], "next_cursor": "same"})
        got = monitor.list_sessions(self.cfg, "tok", self.logs.append)
        self.assertEqual(len(got), 1)

    def test_401_on_first_page_returns_none(self):
        api = FakeApi([sess(i) for i in range(5)], fail_on_page=0, code=401)
        self.assertIsNone(self.run_list(api))
        self.assertTrue(any("401" in m for m in self.logs))

    def test_failure_midwalk_returns_none_not_a_partial_list(self):
        # A partial list is the exact falsehood this function exists to kill,
        # so a page-3 failure must not yield pages 1-2.
        api = FakeApi([sess(i) for i in range(500)], fail_on_page=2, code=500)
        self.assertIsNone(self.run_list(api))
        self.assertTrue(any("page=2" in m for m in self.logs))

    def test_non_dict_body_returns_none(self):
        monitor.api_request = lambda *a, **k: (200, "<html>nope</html>")
        self.assertIsNone(monitor.list_sessions(self.cfg, "tok", self.logs.append))

    def test_cursor_is_url_encoded(self):
        # Real cursors are base64 and can contain '=' and '+'.
        pages = [
            (200, {"data": [sess(1)], "next_cursor": "MTc4NDMy+a/b=="}),
            (200, {"data": [sess(2)], "next_cursor": None}),
        ]
        seen = []
        it = iter(pages)
        def api(cfg, method, path, token, body=None):
            seen.append(path)
            return next(it)
        monitor.api_request = api
        monitor.list_sessions(self.cfg, "tok", self.logs.append)
        self.assertIn("cursor=MTc4NDMy%2Ba%2Fb%3D%3D", seen[1])

    def test_malformed_entries_are_skipped(self):
        monitor.api_request = lambda *a, **k: (
            200, {"data": [sess(1), "junk", None], "next_cursor": None})
        got = monitor.list_sessions(self.cfg, "tok", self.logs.append)
        self.assertEqual(got, [sess(1)])


if __name__ == "__main__":
    unittest.main()
