import json
import tempfile
import unittest
from pathlib import Path

from remote_control.session_fork import (
    cse_id_from_project_dirname,
    do_fork,
    encode_project_dir,
    find_source_jsonl,
    first_cwd,
    rewrite_transcript_lines,
)

# A real worktree path + its observed on-disk project-dir encoding, so the
# encoding stays pinned to what Claude Code actually writes.
WT = "/Users/user/dev/job-search/.claude/worktrees/bridge-cse_01DNt6FHiUuydk6msJV84Wec"
WT_DIR = "-Users-user-dev-job-search--claude-worktrees-bridge-cse-01DNt6FHiUuydk6msJV84Wec"


class EncodeProjectDirTest(unittest.TestCase):
    def test_main_checkout(self):
        self.assertEqual(encode_project_dir("/Users/user/dev/job-search"),
                         "-Users-user-dev-job-search")

    def test_worktree_matches_on_disk_layout(self):
        self.assertEqual(encode_project_dir(WT), WT_DIR)

    def test_one_dash_per_char_not_collapsed(self):
        # `/.claude` -> `--claude` (slash + dot are two separate chars).
        self.assertIn("--claude", encode_project_dir(WT))


class CseIdFromDirnameTest(unittest.TestCase):
    def test_bridge_worktree_dir(self):
        self.assertEqual(cse_id_from_project_dirname(WT_DIR),
                         "cse_01DNt6FHiUuydk6msJV84Wec")

    def test_non_bridge_dir_is_none(self):
        self.assertIsNone(cse_id_from_project_dirname("-Users-user-dev-job-search"))


class FirstCwdTest(unittest.TestCase):
    def test_returns_first_recorded_cwd(self):
        lines = [
            json.dumps({"type": "summary"}),          # no cwd
            json.dumps({"type": "user", "cwd": "/a"}),
            json.dumps({"type": "user", "cwd": "/b"}),
        ]
        self.assertEqual(first_cwd(lines), "/a")

    def test_none_when_absent(self):
        self.assertIsNone(first_cwd([json.dumps({"type": "summary"}), "", "not json"]))


class RewriteTranscriptLinesTest(unittest.TestCase):
    def setUp(self):
        self.lines = [
            json.dumps({"type": "summary"}),                              # no sid/cwd
            json.dumps({"sessionId": "OLD", "cwd": "/old", "type": "user"}),
            json.dumps({"sessionId": "OLD", "type": "assistant"}),        # sid, no cwd
            "",                                                           # blank
            "this is not json",                                          # passthrough
        ]

    def test_rewrites_session_id_everywhere_present(self):
        rw = rewrite_transcript_lines(self.lines, "NEW")
        self.assertEqual(rw.sid_count, 2)
        for s in rw.lines:
            if s.strip().startswith("{") and '"sessionId"' in s:
                self.assertEqual(json.loads(s)["sessionId"], "NEW")

    def test_cwd_rewritten_only_when_new_cwd_given(self):
        unchanged = rewrite_transcript_lines(self.lines, "NEW")
        self.assertEqual(unchanged.cwd_count, 0)
        self.assertEqual(json.loads(unchanged.lines[1])["cwd"], "/old")

        moved = rewrite_transcript_lines(self.lines, "NEW", new_cwd="/new")
        self.assertEqual(moved.cwd_count, 1)
        self.assertEqual(json.loads(moved.lines[1])["cwd"], "/new")

    def test_blank_and_non_json_pass_through(self):
        rw = rewrite_transcript_lines(self.lines, "NEW")
        self.assertEqual(rw.lines[3], "")
        self.assertEqual(rw.lines[4], "this is not json")


class _TmpProjects:
    """Build a fake ~/.claude/projects tree (+ optional bridge worktree) so the
    filesystem paths in do_fork are exercised without touching the real home."""

    def __init__(self, tmp: Path):
        self.root = tmp / "projects"
        self.dev = tmp / "dev"

    def write_session(self, dirname: str, sid: str, lines: list) -> Path:
        d = self.root / dirname
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def make_worktree(self, repo: str, sid: str) -> str:
        wt = self.dev / repo / ".claude" / "worktrees" / f"bridge-{sid}"
        wt.mkdir(parents=True, exist_ok=True)
        return str(wt)


class DoForkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tp = _TmpProjects(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_dir_fork_leaves_original_untouched(self):
        lines = [{"sessionId": "OLD", "cwd": "/work", "type": "user"}]
        src = self.tp.write_session("projA", "OLD", lines)
        before = src.read_text()

        res = do_fork("OLD", projects_root=self.tp.root, dev=str(self.tp.dev),
                      new_sid="NEW")

        self.assertFalse(res.relocated)
        self.assertEqual(res.dest, self.tp.root / "projA" / "NEW.jsonl")
        self.assertTrue(res.dest.exists())
        self.assertEqual(src.read_text(), before)              # original intact
        body = json.loads(res.dest.read_text().splitlines()[0])
        self.assertEqual(body["sessionId"], "NEW")
        self.assertEqual(body["cwd"], "/work")                  # cwd unchanged
        self.assertEqual(res.run_dir, "/work")                  # resume from origin

    def test_into_main_relocates_and_rewrites_cwd(self):
        sid = "cse_ABC123"
        wt = self.tp.make_worktree("coolrepo", sid)
        dirname = encode_project_dir(wt)
        self.tp.write_session(dirname, "uuid1",
                              [{"sessionId": "uuid1", "cwd": wt, "type": "user"}])

        res = do_fork(sid, projects_root=self.tp.root, dev=str(self.tp.dev),
                      new_sid="NEW", into_main=True)

        self.assertTrue(res.relocated)
        self.assertEqual(res.repo, "coolrepo")
        main_cwd = str(self.tp.dev / "coolrepo")
        self.assertEqual(res.run_dir, main_cwd)
        self.assertEqual(res.dest,
                         self.tp.root / encode_project_dir(main_cwd) / "NEW.jsonl")
        body = json.loads(res.dest.read_text().splitlines()[0])
        self.assertEqual(body["cwd"], main_cwd)                 # cwd mirrored to main
        self.assertEqual(res.cwd_count, 1)

    def test_into_main_without_resolvable_repo_errors(self):
        # uuid session not under a bridge worktree -> no repo -> can't mirror main.
        self.tp.write_session("projA", "OLD", [{"sessionId": "OLD", "type": "user"}])
        with self.assertRaises(ValueError):
            do_fork("OLD", projects_root=self.tp.root, dev=str(self.tp.dev),
                    into_main=True)

    def test_dest_dir_overrides_into_main(self):
        self.tp.write_session("projA", "OLD",
                              [{"sessionId": "OLD", "cwd": "/old", "type": "user"}])
        res = do_fork("OLD", projects_root=self.tp.root, dev=str(self.tp.dev),
                      new_sid="NEW", dest_dir="/custom/dir")
        self.assertTrue(res.relocated)
        self.assertEqual(res.run_dir, "/custom/dir")
        self.assertEqual(res.dest,
                         self.tp.root / encode_project_dir("/custom/dir") / "NEW.jsonl")

    def test_dry_run_writes_nothing(self):
        self.tp.write_session("projA", "OLD", [{"sessionId": "OLD", "type": "user"}])
        res = do_fork("OLD", projects_root=self.tp.root, dev=str(self.tp.dev),
                      new_sid="NEW", write=False)
        self.assertFalse(res.written)
        self.assertFalse(res.dest.exists())
        self.assertEqual(res.sid_count, 1)                      # still computed

    def test_missing_session_raises(self):
        with self.assertRaises(FileNotFoundError):
            do_fork("nope", projects_root=self.tp.root, dev=str(self.tp.dev))

    def test_find_source_picks_newest_for_cse(self):
        sid = "cse_DEF456"
        wt = self.tp.make_worktree("r", sid)
        d = encode_project_dir(wt)
        self.tp.write_session(d, "old", [{"sessionId": "old"}])
        newer = self.tp.write_session(d, "new", [{"sessionId": "new"}])
        # make `new` clearly newer
        import os, time
        os.utime(newer, (time.time() + 10, time.time() + 10))
        self.assertEqual(find_source_jsonl(sid, self.tp.root), newer)


if __name__ == "__main__":
    unittest.main()
