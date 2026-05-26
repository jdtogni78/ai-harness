import json
import unittest

from remote_control.session_port import claude as cl
from remote_control.session_port import codex as cx


def _seq_uuid():
    """Deterministic id generator: u0, u1, u2, ... for stable assertions."""
    counter = {"n": -1}

    def gen():
        counter["n"] += 1
        return "u%d" % counter["n"]

    return gen


class EncodeProjectDirTest(unittest.TestCase):
    def test_plain_repo(self):
        self.assertEqual(
            cl.encode_project_dir("/Users/user/dev/AppOne"),
            "-Users-user-dev-AppOne",
        )

    def test_worktree_path_dots_and_underscores(self):
        self.assertEqual(
            cl.encode_project_dir(
                "/Users/user/dev/AppOne/.claude/worktrees/bridge-cse_01AB"),
            "-Users-user-dev-AppOne--claude-worktrees-bridge-cse-01AB",
        )


class MergeTurnsTest(unittest.TestCase):
    def test_merges_consecutive_same_role(self):
        turns = [("user", "hi", "t0"), ("assistant", "a", "t1"),
                 ("assistant", "b", "t2"), ("user", "more", "t3")]
        self.assertEqual(
            cl.merge_turns(turns),
            [("user", "hi", "t0"), ("assistant", "a\n\nb", "t1"), ("user", "more", "t3")],
        )

    def test_drops_leading_assistant_and_empties(self):
        turns = [("assistant", "stray", "t0"), ("user", "  ", "t1"),
                 ("user", "real", "t2")]
        self.assertEqual(cl.merge_turns(turns), [("user", "real", "t2")])


class BuildSessionTest(unittest.TestCase):
    def setUp(self):
        self.turns = [("user", "ask", "t0"), ("assistant", "reply", "t1")]
        self.sid, self.records = cl.build_session(
            "My Title", self.turns, "/Users/x/dev/Repo", new_uuid=_seq_uuid())

    def test_first_record_is_ai_title(self):
        self.assertEqual(self.records[0],
                         {"type": "ai-title", "aiTitle": "My Title", "sessionId": self.sid})

    def test_session_id_and_chain(self):
        self.assertEqual(self.sid, "u0")
        msgs = [r for r in self.records if r.get("type") in ("user", "assistant")]
        self.assertEqual(msgs[0]["parentUuid"], None)
        self.assertEqual(msgs[1]["parentUuid"], msgs[0]["uuid"])

    def test_alternates_and_starts_user(self):
        roles = [r["type"] for r in self.records if r.get("type") in ("user", "assistant")]
        self.assertEqual(roles, ["user", "assistant"])

    def test_message_shapes(self):
        msgs = [r for r in self.records if r.get("type") in ("user", "assistant")]
        self.assertEqual(msgs[0]["message"], {"role": "user", "content": "ask"})
        am = msgs[1]["message"]
        self.assertEqual(am["role"], "assistant")
        self.assertEqual(am["content"], [{"type": "text", "text": "reply"}])
        self.assertEqual(am["model"], cl.DEFAULT_MODEL)

    def test_carries_cwd_and_serializes(self):
        for rec in self.records[1:]:
            self.assertEqual(rec["cwd"], "/Users/x/dev/Repo")
        # whole thing must be JSON-serializable line by line
        for rec in self.records:
            json.loads(json.dumps(rec))

    def test_fallback_timestamp_when_missing(self):
        _, recs = cl.build_session("t", [("user", "x", None)], "/c", new_uuid=_seq_uuid())
        self.assertEqual(recs[1]["timestamp"], cl.FALLBACK_TS)


class ExtractTurnsTest(unittest.TestCase):
    def _lines(self, *objs):
        return [json.dumps(o) for o in objs]

    def test_picks_event_msg_user_and_agent_skips_rest(self):
        lines = self._lines(
            {"type": "session_meta", "payload": {"id": "x", "cwd": "/c"}},
            {"type": "event_msg", "timestamp": "t0",
             "payload": {"type": "user_message", "message": "hello"}},
            {"type": "response_item", "payload": {"type": "reasoning"}},
            {"type": "event_msg", "timestamp": "t1",
             "payload": {"type": "agent_message", "message": "hi there"}},
            {"type": "event_msg", "payload": {"type": "token_count"}},
        )
        self.assertEqual(
            cx.extract_turns(lines),
            [("user", "hello", "t0"), ("assistant", "hi there", "t1")],
        )

    def test_skips_agents_preamble_user_message(self):
        lines = self._lines(
            {"type": "event_msg", "timestamp": "t0",
             "payload": {"type": "user_message",
                         "message": "# AGENTS.md instructions for /x\n<INSTRUCTIONS>..."}},
            {"type": "event_msg", "timestamp": "t1",
             "payload": {"type": "user_message", "message": "the real ask"}},
        )
        self.assertEqual(cx.extract_turns(lines), [("user", "the real ask", "t1")])

    def test_tolerates_bad_json_lines(self):
        lines = ["{not json", json.dumps(
            {"type": "event_msg", "timestamp": "t",
             "payload": {"type": "agent_message", "message": "ok"}})]
        self.assertEqual(cx.extract_turns(lines), [("assistant", "ok", "t")])


class MetaAndIndexTest(unittest.TestCase):
    def test_meta_of(self):
        lines = [json.dumps({"type": "session_meta",
                             "payload": {"id": "abc", "cwd": "/Users/x/dev/Repo"}})]
        self.assertEqual(cx.meta_of(lines), ("abc", "/Users/x/dev/Repo"))

    def test_meta_of_absent(self):
        self.assertEqual(cx.meta_of([json.dumps({"type": "event_msg"})]), ("", ""))

    def test_parse_index(self):
        text = ("\n".join([
            json.dumps({"id": "a", "thread_name": "First", "updated_at": "2026-01-01T00:00:00Z"}),
            "",
            json.dumps({"id": "b", "thread_name": "Second", "updated_at": "2026-02-01T00:00:00Z"}),
        ]))
        idx = cx.parse_index(text)
        self.assertEqual(idx["a"]["title"], "First")
        self.assertEqual(idx["b"]["updated_at"], "2026-02-01T00:00:00Z")


class SelectTest(unittest.TestCase):
    def setUp(self):
        S = cx.CodexSession
        self.sessions = [
            S("019e-aaa", "Run security code scan", "/Users/x/dev/AppOne", "p1", "2026-05-01T00:00:00Z", False),
            S("019e-bbb", "Automate pen test execution", "/Users/x/dev/AppOne", "p2", "2026-05-03T00:00:00Z", False),
            S("019e-ccc", "Unrelated job-search chat", "/Users/x/dev/job-search", "p3", "2026-05-02T00:00:00Z", True),
        ]

    def test_id_prefix(self):
        got = cx.select(self.sessions, ids=["019e-bbb"])
        self.assertEqual([s.id for s in got], ["019e-bbb"])

    def test_match_regex_case_insensitive(self):
        got = cx.select(self.sessions, match="security|pen test")
        self.assertEqual({s.id for s in got}, {"019e-aaa", "019e-bbb"})

    def test_cwd_filter(self):
        got = cx.select(self.sessions, cwd="/Users/x/dev/job-search")
        self.assertEqual([s.id for s in got], ["019e-ccc"])

    def test_sorted_newest_first_and_limit(self):
        got = cx.select(self.sessions, limit=2)
        self.assertEqual([s.id for s in got], ["019e-bbb", "019e-ccc"])


if __name__ == "__main__":
    unittest.main()
