"""Subprocess tests for skills/manage/scripts/workers.sh's retitle-worker.

Follows the pattern used by tests/test_work_start.py and friends: spawn the
real script via subprocess, point it at a scratch state dir/manager id, and
stub out the `python3 -m remote_control titles set` call it shells out to so
the test never touches the real API or ~/.ai-harness/manager/.
"""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKERS_SH = REPO_ROOT / "skills" / "manage" / "scripts" / "workers.sh"


def _make_stub_ai_harness_dir(tmp_path: Path) -> Path:
    """A fake AI_HARNESS_DIR whose `remote_control` package just echoes the
    argv it was called with as JSON, so `titles set` calls are observable
    without hitting the real CLI or network."""
    stub_dir = tmp_path / "stub_ai_harness"
    pkg = stub_dir / "remote_control"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(textwrap.dedent("""\
        import json, sys
        print(json.dumps(sys.argv[1:]))
        """))
    return stub_dir


class RetitleWorkerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)
        self.state_dir = tmp_path / "manager_state"
        self.ai_harness_dir = _make_stub_ai_harness_dir(tmp_path)
        self.env = dict(os.environ)
        self.env.update({
            "MANAGER_STATE_DIR": str(self.state_dir),
            "MANAGER_CSE_ID": "cse_test_mgr",
            "AI_HARNESS_DIR": str(self.ai_harness_dir),
        })

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(WORKERS_SH), *args],
            env=self.env, capture_output=True, text=True)

    def test_with_ticket_chain_unchanged(self):
        reg = self._run("register", "w1", "/tmp/d1", "--ticket", "42",
                         "--brief", "fix the thing")
        self.assertEqual(reg.returncode, 0, reg.stderr)

        res = self._run("retitle-worker", "w1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")

        argv = json.loads(res.stdout)
        self.assertEqual(argv, [
            "titles", "set", "--id", "w1", "--cwd", "/tmp/d1",
            "--sub", "MGR1-W1", "--sub", "#42", "fix the thing",
        ])

    def test_without_ticket_degrades_gracefully(self):
        # Simulate a legacy/registered-without-ticket worker (per #97's
        # real-world repro): a folded record whose ticket is "".
        reg = self._run("register", "w1", "/tmp/d1", "--ticket", "42",
                         "--brief", "fix the thing")
        self.assertEqual(reg.returncode, 0, reg.stderr)

        state_file = self.state_dir / "cse_test_mgr.jsonl"
        rec = {"event": "register", "worker": "w2", "dir": "/tmp/d2",
               "ticket": "", "brief": "no ticket here",
               "worker_ord": 2, "ts": "2026-01-01T00:00:00Z"}
        with state_file.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

        res = self._run("retitle-worker", "w2")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no ticket recorded", res.stderr)

        argv = json.loads(res.stdout)
        self.assertEqual(argv, [
            "titles", "set", "--id", "w2", "--cwd", "/tmp/d2",
            "--sub", "MGR1-W2", "no ticket here",
        ])
        self.assertNotIn("#", " ".join(argv))


class RetitleManagerTest(unittest.TestCase):
    """Manager `retitle`: nick from session identity (no --cwd, #105) and never
    a ticket bracket (#109 — managers carry no ticket #)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp_path = Path(self._tmp.name)
        self.state_dir = tmp_path / "manager_state"
        self.ai_harness_dir = _make_stub_ai_harness_dir(tmp_path)
        self.env = dict(os.environ)
        self.env.update({
            "MANAGER_STATE_DIR": str(self.state_dir),
            "MANAGER_CSE_ID": "cse_test_mgr",
            "AI_HARNESS_DIR": str(self.ai_harness_dir),
        })

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(WORKERS_SH), *args],
            env=self.env, capture_output=True, text=True)

    def test_no_ticket_bracket_and_no_cwd(self):
        # A manager title never carries a [#<ticket>] sub (#109), and crucially
        # no --cwd (nick must come from `--id <mgr>` session identity, not the
        # dir retitle happened to run in -- the #105 churn fix).
        res = self._run("retitle", "manage the thing")
        self.assertEqual(res.returncode, 0, res.stderr)
        argv = json.loads(res.stdout)
        self.assertEqual(argv, [
            "titles", "set", "--id", "cse_test_mgr",
            "--sub", "MGR-1", "manage the thing (0 workers)",
        ])
        self.assertNotIn("--cwd", argv)
        self.assertNotIn("#", " ".join(argv))

    def test_epic_feature_is_gone(self):
        # #109 reversed the #105 manager-epic bracket: neither the `mgr-epic`
        # subcommand nor `retitle --epic` exists any more, and no code path
        # can put a `[#` bracket on a manager title.
        self.assertNotEqual(self._run("mgr-epic", "get").returncode, 0)
        self.assertNotEqual(
            self._run("retitle", "manage the thing", "--epic", "99").returncode, 0)
        argv = json.loads(self._run("retitle", "manage the thing").stdout)
        self.assertNotIn("#", " ".join(argv))


if __name__ == "__main__":
    unittest.main()
