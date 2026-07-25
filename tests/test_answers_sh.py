"""Subprocess tests for skills/manage/scripts/answers.sh's file logic.

Mirrors tests/test_workers_sh.py: run the real script via subprocess pointed
at a scratch ANSWERS_FILE / state dir, with --no-inbox --no-notify so the test
never touches the network, the real iCloud file, or spawns a session.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSWERS_SH = REPO_ROOT / "skills" / "manage" / "scripts" / "answers.sh"


class AnswersFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.answers_file = tmp / "cloud" / "ANSWERS.md"
        self.env = dict(os.environ)
        self.env.update({
            "ANSWERS_FILE": str(self.answers_file),
            "ANSWERS_STATE_DIR": str(tmp / "state"),
        })

    def _post(self, mgr, subject, q, a, ticket=None, now=None):
        args = ["post", "--mgr", mgr, "--subject", subject, "--q", q, "--a", a,
                "--no-inbox", "--no-notify"]
        if ticket:
            args += ["--ticket", str(ticket)]
        env = dict(self.env)
        if now:
            env["ANSWERS_NOW"] = now
        return subprocess.run(["bash", str(ANSWERS_SH), *args],
                              env=env, capture_output=True, text=True)

    def _text(self):
        return self.answers_file.read_text()

    def test_first_post_creates_file_and_section(self):
        r = self._post("MGR-13", "Monarch history-match",
                       "How far back?", "To 2019.", ticket=44,
                       now="2026-07-24 14:03")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self._text()
        self.assertIn("# Boss Answers Feed", text)
        self.assertIn("## MGR-13 — Monarch history-match", text)
        self.assertIn("### 2026-07-24 14:03 · [MGR-13 / #44]", text)
        self.assertIn("**Q:** How far back?", text)
        self.assertIn("**A:** To 2019.", text)

    def test_newest_entry_on_top_within_section(self):
        self._post("MGR-13", "Monarch", "Q1", "A1", now="2026-07-24 10:00")
        self._post("MGR-13", "Monarch", "Q2", "A2", now="2026-07-24 11:00")
        text = self._text()
        # Only one section header for the subject.
        self.assertEqual(text.count("## MGR-13 — Monarch"), 1)
        # Q2 (newer) appears before Q1 in the file.
        self.assertLess(text.index("**Q:** Q2"), text.index("**Q:** Q1"))

    def test_new_subject_creates_new_section_on_top(self):
        self._post("MGR-13", "First subject", "Q1", "A1", now="2026-07-24 10:00")
        self._post("MGR-14", "Second subject", "Q2", "A2", now="2026-07-24 11:00")
        text = self._text()
        self.assertIn("## MGR-13 — First subject", text)
        self.assertIn("## MGR-14 — Second subject", text)
        # Newer subject's section floats above the older one.
        self.assertLess(text.index("## MGR-14 — Second subject"),
                        text.index("## MGR-13 — First subject"))

    def test_idempotent_duplicate_is_noop(self):
        self._post("MGR-13", "Monarch", "Q1", "A1", now="2026-07-24 10:00")
        self._post("MGR-13", "Monarch", "Q1", "A1", now="2026-07-24 10:00")
        text = self._text()
        self.assertEqual(text.count("**Q:** Q1"), 1)

    def test_no_ticket_tag_omits_ticket(self):
        r = self._post("MGR-9", "No ticket subj", "Q", "A", now="2026-07-24 09:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("### 2026-07-24 09:00 · [MGR-9]", self._text())

    def test_missing_required_arg_fails(self):
        r = subprocess.run(
            ["bash", str(ANSWERS_SH), "post", "--mgr", "MGR-1",
             "--q", "q", "--a", "a", "--no-inbox", "--no-notify"],
            env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--subject", r.stderr)

    def test_concurrent_posts_do_not_lose_entries(self):
        # Fire several posts to the SAME section concurrently; the mkdir lock
        # must serialize the read-modify-write so every entry survives.
        procs = []
        for i in range(6):
            env = dict(self.env)
            env["ANSWERS_NOW"] = f"2026-07-24 12:{i:02d}"
            procs.append(subprocess.Popen(
                ["bash", str(ANSWERS_SH), "post", "--mgr", "MGR-5",
                 "--subject", "Race", "--q", f"Q{i}", "--a", f"A{i}",
                 "--no-inbox", "--no-notify"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate()
        text = self._text()
        for i in range(6):
            self.assertIn(f"**Q:** Q{i}", text, f"lost entry Q{i}")
        self.assertEqual(text.count("## MGR-5 — Race"), 1)


if __name__ == "__main__":
    unittest.main()
