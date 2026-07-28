"""Subprocess tests for skills/manage/scripts/registry.sh.

Mirrors tests/test_answers_sh.py / tests/test_workers_sh.py: run the real
script via subprocess pointed at a scratch MANAGER_STATE_DIR, with the live
session list injected via REGISTRY_LIVE_JSON so the suite never touches the
network or the real ~/.ai-harness/manager state.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_SH = REPO_ROOT / "skills" / "manage" / "scripts" / "registry.sh"


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "manager"
        self.env = dict(os.environ)
        self.env.update({
            "MANAGER_STATE_DIR": str(self.state_dir),
            "REGISTRY_NOW": "2026-07-27T20:00:00Z",
        })

    def run_reg(self, *args, env_extra=None):
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["bash", str(REGISTRY_SH), *args],
                              env=env, capture_output=True, text=True)

    def register(self, cse, ord_, kind="domain", name="m", goals="g",
                 responsibilities="skills", board=None, project=None, force=False):
        args = ["register", "--kind", kind, "--name", name, "--goals", goals,
                "--responsibilities", responsibilities, "--cse", cse,
                "--mgr-ord", str(ord_)]
        if board:
            args += ["--board", str(board)]
        if project:
            args += ["--project", project]
        if force:
            args += ["--force"]
        return self.run_reg(*args)

    def live_file(self, sessions):
        p = self.tmp / "live.json"
        p.write_text(json.dumps(sessions))
        return str(p)


class RegisterListTest(RegistryTestBase):
    def test_register_and_list_json(self):
        r = self.register("cse_" + "A" * 12, 20, name="skill-manager",
                          goals="own skills", responsibilities="skills")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.run_reg("list", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cse_id"], "cse_" + "A" * 12)
        self.assertEqual(rows[0]["mgr_ord"], 20)
        self.assertEqual(rows[0]["kind"], "domain")
        self.assertEqual(rows[0]["responsibilities"], ["skills"])
        self.assertEqual(rows[0]["status"], "active")

    def test_kind_required_and_validated(self):
        r = self.run_reg("register", "--kind", "bogus", "--name", "m",
                         "--goals", "g", "--responsibilities", "skills",
                         "--cse", "cse_x", "--mgr-ord", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--kind", r.stderr)

    def test_project_needs_board_or_project(self):
        r = self.run_reg("register", "--kind", "project", "--name", "m",
                         "--goals", "g", "--responsibilities", "deck",
                         "--cse", "cse_" + "P" * 12, "--mgr-ord", "3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--board", r.stderr)

    def test_non_cse_id_refused(self):
        r = self.register("not-a-cse", 5)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-cse", r.stderr)

    def test_reregister_updates_and_preserves_registered_at(self):
        cse = "cse_" + "R" * 12
        self.register(cse, 20, goals="first")
        # Re-register with a later timestamp and new goals.
        self.run_reg("register", "--kind", "domain", "--name", "m",
                     "--goals", "second", "--responsibilities", "skills",
                     "--cse", cse, "--mgr-ord", "20",
                     env_extra={"REGISTRY_NOW": "2026-07-28T00:00:00Z"})
        rows = json.loads(self.run_reg("list", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["goals"], "second")
        self.assertEqual(rows[0]["registered_at"], "2026-07-27T20:00:00Z")


class NormalizationTest(RegistryTestBase):
    def test_canon_lowercases_trims_and_flags_unknown(self):
        out = json.loads(self.run_reg("canon", "Skills, skill , INFRA,foobar").stdout)
        self.assertEqual(out["canon"], ["skills", "infra", "foobar"])
        self.assertEqual(out["unknown"], ["foobar"])

    def test_lookup_matches_across_spellings(self):
        cse = "cse_" + "A" * 12
        self.register(cse, 20, responsibilities="Skills")   # stored canonical
        for query in ("skills", "Skill ", "SKILLS", "skill"):
            r = self.run_reg("lookup", "--responsibility", query)
            self.assertEqual(r.returncode, 0, f"{query}: {r.stderr}")
            self.assertEqual(r.stdout.strip(), cse, query)

    def test_unknown_responsibility_warns_but_registers(self):
        r = self.register("cse_" + "U" * 12, 30, responsibilities="wizardry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("controlled vocabulary", r.stderr)

    def test_lookup_no_owner_exits_4(self):
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        r = self.run_reg("lookup", "--responsibility", "trading")
        self.assertEqual(r.returncode, 4)


class DomainExclusivityTest(RegistryTestBase):
    def test_second_domain_owner_refused(self):
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        r = self.register("cse_" + "B" * 12, 21, responsibilities="skill")  # alias
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("conflict", r.stderr)

    def test_force_overrides_conflict(self):
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        r = self.register("cse_" + "B" * 12, 21, responsibilities="skills", force=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("overriding", r.stderr)

    def test_project_managers_may_overlap(self):
        self.register("cse_" + "A" * 12, 20, kind="project", board=3,
                      responsibilities="deck")
        r = self.register("cse_" + "B" * 12, 21, kind="project", board=4,
                          responsibilities="deck")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_lookup_prefers_domain_owner_over_project(self):
        self.register("cse_proj00000000", 21, kind="project", board=3,
                      responsibilities="skills")   # project claims skills too
        self.register("cse_domain000000", 20, kind="domain",
                      responsibilities="skills")
        r = self.run_reg("lookup", "--responsibility", "skills")
        self.assertEqual(r.stdout.strip(), "cse_domain000000", r.stderr)


class SetReportTest(RegistryTestBase):
    def test_reported_requires_note_and_writes_progress(self):
        cse = "cse_" + "A" * 12
        self.register(cse, 20)
        r = self.run_reg("set-report", "--cse", cse, "--note", "shipped #144")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.run_reg("list", "--json").stdout)
        self.assertEqual(rows[0]["last_note"], "shipped #144")
        self.assertEqual(rows[0]["last_report_status"], "reported")
        prog = (self.state_dir / "progress.jsonl").read_text().strip().splitlines()
        entry = json.loads(prog[-1])
        self.assertEqual(entry["note"], "shipped #144")
        self.assertEqual(entry["status"], "reported")

    def test_reported_without_note_fails(self):
        cse = "cse_" + "A" * 12
        self.register(cse, 20)
        r = self.run_reg("set-report", "--cse", cse)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--note", r.stderr)

    def test_no_response_status_needs_no_note(self):
        # A non-response must be recordable so a busy manager never renders as
        # silence (#145 gap 3).
        cse = "cse_" + "A" * 12
        self.register(cse, 20)
        r = self.run_reg("set-report", "--cse", cse, "--status", "unreachable-busy")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.run_reg("list", "--json").stdout)
        self.assertEqual(rows[0]["last_report_status"], "unreachable-busy")

    def test_bad_status_rejected(self):
        cse = "cse_" + "A" * 12
        self.register(cse, 20)
        r = self.run_reg("set-report", "--cse", cse, "--status", "bogus", "--note", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--status", r.stderr)


class RetireTest(RegistryTestBase):
    def test_retire_drops_from_active_list(self):
        cse = "cse_" + "A" * 12
        self.register(cse, 20)
        self.run_reg("retire", "--cse", cse, "--reason", "done")
        self.assertEqual(len(json.loads(self.run_reg("list", "--json").stdout)), 0)
        all_rows = json.loads(self.run_reg("list", "--all", "--json").stdout)
        self.assertEqual(all_rows[0]["status"], "retired")

    def test_retired_responsibility_frees_domain(self):
        # After retiring, a new domain manager can claim the freed responsibility.
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        self.run_reg("retire", "--cse", "cse_" + "A" * 12)
        r = self.register("cse_" + "B" * 12, 21, responsibilities="skills")
        self.assertEqual(r.returncode, 0, r.stderr)


class AuditTest(RegistryTestBase):
    def _two_managers(self):
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        self.register("cse_" + "B" * 12, 21, kind="project", board=3,
                      responsibilities="deck")

    def test_clean_ledger_exit_0(self):
        self._two_managers()
        live = self.live_file([
            {"id": "cse_" + "A" * 12, "title": "[X][MGR-20] skills"},
            {"id": "cse_" + "B" * 12, "title": "[X][MGR-21] deck"},
        ])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("consistent", r.stdout)

    def test_archived_holder_exit_1(self):
        self._two_managers()
        live = self.live_file([{"id": "cse_" + "A" * 12, "title": "[X][MGR-20] skills"}])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 1)
        self.assertIn("ARCHIVED", r.stdout)

    def test_unregistered_live_manager_exit_1(self):
        self._two_managers()
        live = self.live_file([
            {"id": "cse_" + "A" * 12, "title": "[X][MGR-20] skills"},
            {"id": "cse_" + "B" * 12, "title": "[X][MGR-21] deck"},
            {"id": "cse_" + "C" * 12, "title": "[X][MGR-99] rogue"},
        ])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO active registry record", r.stdout)

    def test_worker_bracket_not_flagged_as_manager(self):
        # A live WORKER title [MGRn-Wk] must NOT be mistaken for an unregistered
        # manager (a loose regex matched "[MGR13-W1]" and flagged it).
        self._two_managers()
        live = self.live_file([
            {"id": "cse_" + "A" * 12, "title": "[FE.m5][MGR-20] skills"},
            {"id": "cse_" + "B" * 12, "title": "[FE.m5][MGR-21] deck"},
            {"id": "cse_wrk000000000", "title": "[FE.m5][MGR13-W1][#44] extraction"},
        ])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_cross_host_manager_not_flagged(self):
        # A live [MGR-N] on ANOTHER host (nick host segment != this host) lives
        # in its own host's registry -- don't flag it here.
        self._two_managers()
        live = self.live_file([
            {"id": "cse_" + "A" * 12, "title": "[FE.m5][MGR-20] skills"},
            {"id": "cse_" + "B" * 12, "title": "[FE.m5][MGR-21] deck"},
            {"id": "cse_mini00000000", "title": "[DEV.mini][MGR-100] peer dispatcher"},
        ])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live,
                                             "REMOTE_CONTROL_HOST": "m5"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_empty_live_list_refused_exit_2(self):
        self._two_managers()
        live = self.live_file([])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing to audit", r.stderr)

    def test_domain_double_claim_exit_1(self):
        # Two DOMAIN managers both owning skills (planted via --force) is a fault
        # even when both sessions are live.
        self.register("cse_" + "A" * 12, 20, responsibilities="skills")
        self.register("cse_" + "B" * 12, 21, responsibilities="skills", force=True)
        live = self.live_file([
            {"id": "cse_" + "A" * 12, "title": "[X][MGR-20] skills"},
            {"id": "cse_" + "B" * 12, "title": "[X][MGR-21] skills"},
        ])
        r = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 1)
        self.assertIn("DOUBLE-CLAIMED", r.stdout)

    def test_fix_retires_archived_holder(self):
        self._two_managers()
        live = self.live_file([{"id": "cse_" + "A" * 12, "title": "[X][MGR-20] skills"}])
        r = self.run_reg("audit", "--fix", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # The archived B is now retired; a re-audit against A-only is clean.
        r2 = self.run_reg("audit", env_extra={"REGISTRY_LIVE_JSON": live})
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)


class ProjectsTest(RegistryTestBase):
    def setUp(self):
        super().setUp()
        boards = self.tmp / "boards.json"
        boards.write_text(json.dumps([
            {"board": 2, "name": "ai-harness"},
            {"board": 3, "name": "deck"},
        ]))
        supp = self.tmp / "supp.jsonl"
        supp.write_text(
            '{"name":"skills domain","description":"skills","responsibilities":["skills"]}\n'
            '{"name":"answers feed","description":"feed","responsibilities":["coordination"]}\n'
        )
        self.env.update({
            "REGISTRY_BOARDS_JSON": str(boards),
            "SYSTEM_INITIATIVES_FILE": str(supp),
        })

    def test_owned_vs_spin_candidate(self):
        self.register("cse_domain000000", 20, responsibilities="skills")
        self.register("cse_proj00000000", 21, kind="project", board=3,
                      responsibilities="deck")
        rows = json.loads(self.run_reg("projects", "--json").stdout)
        by_name = {r["name"]: r for r in rows}
        self.assertFalse(by_name["deck"]["spin_candidate"])         # board 3 owned
        self.assertEqual(by_name["deck"]["owner"], "cse_proj00000000")
        self.assertTrue(by_name["ai-harness"]["spin_candidate"])    # board 2 unowned
        self.assertFalse(by_name["skills domain"]["spin_candidate"])  # initiative owned
        self.assertTrue(by_name["answers feed"]["spin_candidate"])  # initiative unowned


class ConcurrencyTest(RegistryTestBase):
    def test_concurrent_registers_do_not_lose_entries(self):
        procs = []
        for i in range(6):
            env = dict(self.env)
            env["REGISTRY_NOW"] = f"2026-07-27T20:0{i}:00Z"
            procs.append(subprocess.Popen(
                ["bash", str(REGISTRY_SH), "register", "--kind", "project",
                 "--name", f"m{i}", "--goals", "g", "--responsibilities", "deck",
                 "--board", "3", "--cse", f"cse_{'C'*8}{i:04d}", "--mgr-ord", str(30 + i)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate()
        rows = json.loads(self.run_reg("list", "--json").stdout)
        self.assertEqual(len(rows), 6, "lost a concurrent register")


if __name__ == "__main__":
    unittest.main()
