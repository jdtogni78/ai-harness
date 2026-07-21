import tempfile
import unittest
from pathlib import Path

from remote_control import oneshot_reaper
from remote_control.oneshot_reaper import (
    ARCHIVED,
    Candidate,
    KEEP_BUSY,
    KEEP_NO_CAPACITY,
    KEEP_NO_SESSION_ID,
    KEEP_PROTECTED,
    KEEP_SESSION_LIVE,
    KEEP_SESSION_UNKNOWN,
    KEEP_TOO_YOUNG,
    REAP,
    classify,
    parse_session_ids,
    plan,
    read_log_session_id,
    sweep,
)
from remote_control import procutil


DEAD = Candidate(pid=999, name="local-m5-deadbeef", capacity=0,
                 session_id="cse_" + "A" * 22, age_secs=10_000)


def cand(**kw):
    return DEAD._replace(**kw)


class ClassifyTest(unittest.TestCase):
    """The kill/keep matrix. Every branch must default to KEEP."""

    def test_both_signals_agree_dead_is_the_only_reap(self):
        d = classify(DEAD, ARCHIVED)
        self.assertTrue(d.reap)
        self.assertEqual(d.reason, REAP)

    def test_capacity_nonzero_keeps_even_if_session_archived(self):
        # Signal 1 says a session is attached. Disagreement -> keep.
        d = classify(cand(capacity=1), ARCHIVED)
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_BUSY)

    def test_session_not_archived_keeps_even_if_capacity_zero(self):
        # Signal 2 says the session is live. Disagreement -> keep.
        for status in ("active", "paused", ""):
            with self.subTest(status=status):
                d = classify(DEAD, status)
                self.assertFalse(d.reap)
                self.assertEqual(d.reason, KEEP_SESSION_LIVE)

    def test_session_absent_from_api_keeps(self):
        # The truncated-list failure mode: absent must never mean dead.
        d = classify(DEAD, None)
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_SESSION_UNKNOWN)

    def test_no_capacity_line_keeps(self):
        d = classify(cand(capacity=-1), ARCHIVED)
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_NO_CAPACITY)

    def test_unmappable_server_keeps(self):
        d = classify(cand(session_id=None), ARCHIVED)
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_NO_SESSION_ID)

    def test_young_server_keeps(self):
        d = classify(cand(age_secs=5), ARCHIVED, min_age_secs=300)
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_TOO_YOUNG)
        # ...and is reaped once it ages past the guard.
        self.assertTrue(classify(cand(age_secs=301), ARCHIVED,
                                 min_age_secs=300).reap)

    def test_protected_pid_wins_over_every_dead_signal(self):
        d = classify(DEAD, ARCHIVED, protected_pids=[DEAD.pid])
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_PROTECTED)

    def test_protected_name_wins_over_every_dead_signal(self):
        d = classify(cand(name="m5-dev"), ARCHIVED, protected_names=["m5-dev"])
        self.assertFalse(d.reap)
        self.assertEqual(d.reason, KEEP_PROTECTED)


class ParseTest(unittest.TestCase):
    def test_strips_ansi_and_normalises_prefixes(self):
        sid = "01Ut3CVUvwHDPdie3jVvLJJh" + "xx"
        log = (f"\x1b[6A\x1b[Jsome url .../code/session_{sid}\n"
               f"\x1b[1A\x1b[JSession failed cse_{sid}\n")
        self.assertEqual(parse_session_ids(log), ["cse_" + sid])

    def test_returns_first_seen_order_and_dedupes(self):
        a, b = "A" * 24, "B" * 24
        self.assertEqual(
            parse_session_ids(f"cse_{a} cse_{b} cse_{a}"),
            ["cse_" + a, "cse_" + b],
        )

    def test_empty_log_yields_nothing(self):
        self.assertEqual(parse_session_ids(""), [])

    def test_read_log_session_id_takes_the_last(self):
        a, b = "A" * 24, "B" * 24
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.log"
            p.write_text(f"cse_{a}\nlater\ncse_{b}\n")
            self.assertEqual(read_log_session_id(p), "cse_" + b)

    def test_read_log_session_id_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_log_session_id(Path(td) / "nope.log"))


class SweepTest(unittest.TestCase):
    def _sweep(self, candidates, sessions, **kw):
        termed, logs = [], []
        by_name = {c.name: c for c in candidates}
        sweep(
            oneshots=[(c.pid, c.name) for c in candidates],
            logdir=Path("/nonexistent"),
            sessions=sessions,
            read_capacity=lambda p: by_name[Path(p).stem].capacity,
            process_age=lambda pid: next(
                c.age_secs for c in candidates if c.pid == pid),
            term=termed.append,
            log=logs.append,
            **kw,
        )
        return termed, logs

    def setUp(self):
        # read_log_session_id hits the filesystem; stub it to the candidate's id.
        self._orig = oneshot_reaper.read_log_session_id
        oneshot_reaper.read_log_session_id = (
            lambda p, **kw: self._ids.get(Path(p).stem))

    def tearDown(self):
        oneshot_reaper.read_log_session_id = self._orig

    def test_reaps_only_the_agreed_dead(self):
        dead = cand(pid=1, name="local-m5-dead", session_id="cse_" + "D" * 22)
        busy = cand(pid=2, name="local-m5-busy", capacity=1,
                    session_id="cse_" + "B" * 22)
        live = cand(pid=3, name="local-m5-live", session_id="cse_" + "L" * 22)
        self._ids = {c.name: c.session_id for c in (dead, busy, live)}
        termed, _ = self._sweep(
            [dead, busy, live],
            sessions=[{"id": dead.session_id, "status": "archived"},
                      {"id": busy.session_id, "status": "archived"},
                      {"id": live.session_id, "status": "active"}],
        )
        self.assertEqual(termed, [1])

    def test_none_session_list_aborts_the_whole_sweep(self):
        # An API failure must never be read as "no sessions exist".
        dead = cand(pid=1, name="local-m5-dead")
        self._ids = {dead.name: dead.session_id}
        termed, logs = self._sweep([dead], sessions=None)
        self.assertEqual(termed, [])
        self.assertTrue(any("skipping sweep" in m for m in logs))

    def test_empty_session_list_still_keeps_unmappable(self):
        # No sessions at all -> every id is "not in API" -> keep, not kill.
        dead = cand(pid=1, name="local-m5-dead")
        self._ids = {dead.name: dead.session_id}
        termed, _ = self._sweep([dead], sessions=[])
        self.assertEqual(termed, [])

    def test_max_per_sweep_caps_and_says_so(self):
        cands = [cand(pid=i, name=f"local-m5-d{i}", session_id=f"cse_{i:0>24}")
                 for i in range(1, 6)]
        self._ids = {c.name: c.session_id for c in cands}
        termed, logs = self._sweep(
            cands,
            sessions=[{"id": c.session_id, "status": "archived"} for c in cands],
            max_per_sweep=2,
        )
        self.assertEqual(len(termed), 2)
        self.assertTrue(any("capping this sweep" in m for m in logs))

    def test_protected_name_survives_a_sweep(self):
        dev = cand(pid=1, name="m5-dev")
        self._ids = {dev.name: dev.session_id}
        termed, _ = self._sweep(
            [dev],
            sessions=[{"id": dev.session_id, "status": "archived"}],
            protected_names=["m5-dev"],
        )
        self.assertEqual(termed, [])

    def test_term_failure_does_not_abort_remaining_reaps(self):
        a = cand(pid=1, name="local-m5-a", session_id="cse_" + "A" * 22)
        b = cand(pid=2, name="local-m5-b", session_id="cse_" + "B" * 22)
        self._ids = {c.name: c.session_id for c in (a, b)}
        termed, logs = [], []
        def boom(pid):
            if pid == 1:
                raise OSError("no such process")
            termed.append(pid)
        sweep(
            oneshots=[(a.pid, a.name), (b.pid, b.name)],
            logdir=Path("/nonexistent"),
            sessions=[{"id": a.session_id, "status": "archived"},
                      {"id": b.session_id, "status": "archived"}],
            read_capacity=lambda p: 0,
            process_age=lambda pid: 10_000,
            term=boom,
            log=logs.append,
        )
        self.assertEqual(termed, [2])
        self.assertTrue(any("failed" in m for m in logs))


class PlanTest(unittest.TestCase):
    def test_preserves_input_order(self):
        cands = [cand(pid=i, name=f"n{i}", session_id=f"cse_{i:0>24}")
                 for i in range(3)]
        decisions = plan(cands, {})
        self.assertEqual([d.candidate.pid for d in decisions], [0, 1, 2])


class ParseEtimeTest(unittest.TestCase):
    """macOS ps only has the formatted ``etime``; asking for ``etimes`` errors
    out entirely, which silently pinned every age to 0 and would have made the
    reaper's min-age guard permanent (caught in a live dry-run)."""

    def test_formats(self):
        cases = {
            "5": 5.0,                 # ss
            "01:20": 80.0,            # mm:ss
            "10:20:30": 37230.0,      # hh:mm:ss
            "1-00:00:00": 86400.0,    # dd-hh:mm:ss
            "03-07:56:00": 287760.0,  # the real m5-dev reading
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(procutil.parse_etime(text), want)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(procutil.parse_etime("  01:20 \n"), 80.0)

    def test_unparseable_is_none(self):
        for text in ("", "   ", "junk", "a:b", "1-junk", "1:2:3:4"):
            with self.subTest(text=text):
                self.assertIsNone(procutil.parse_etime(text))

    def test_process_age_of_bogus_pid_is_zero_not_a_crash(self):
        # 0.0 keeps the process (too young to judge) -- the safe direction.
        self.assertEqual(procutil.process_age_secs(2 ** 30), 0.0)


class OneshotDiscriminatorTest(unittest.TestCase):
    """``--capacity 1`` must match one-shots and never a supervisor server."""

    def _names(self, lines):
        found = []
        for line in lines:
            m = procutil._ONESHOT_RE.search(line)
            if m:
                found.append(m.group(1))
        return found

    def test_matches_oneshot_forms(self):
        self.assertEqual(self._names([
            "/Users/x/.local/bin/claude remote-control --name local-m5-abc "
            "--spawn worktree --capacity 1 --permission-mode bypassPermissions",
            "/usr/bin/claude remote-control --name w-thing --spawn same-dir "
            "--capacity 1",
        ]), ["local-m5-abc", "w-thing"])

    def test_never_matches_supervisor_owned_servers(self):
        # spawn_argv never emits --capacity; this is the structural guarantee
        # that m5-dev can't become a reap candidate.
        self.assertEqual(self._names([
            "/Users/x/.local/bin/claude remote-control --name m5-dev "
            "--spawn same-dir --permission-mode bypassPermissions "
            "--create-session-in-dir",
            "/Users/x/.local/bin/claude remote-control --name m5-app "
            "--spawn same-dir --permission-mode bypassPermissions "
            "--no-create-session-in-dir",
        ]), [])

    def test_does_not_match_capacity_other_than_one(self):
        self.assertEqual(self._names([
            "claude remote-control --name x --spawn worktree --capacity 10",
        ]), [])

    def test_does_not_match_tooling_merely_mentioning_a_server(self):
        self.assertEqual(self._names([
            "grep --name local-m5-abc --capacity 1 somefile",
            "python3 -m remote_control new-session --name local-m5-abc",
        ]), [])


if __name__ == "__main__":
    unittest.main()
