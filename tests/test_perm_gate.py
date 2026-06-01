import json
import tempfile
import unittest
from pathlib import Path

from remote_control.config import PermGateConfig
from remote_control.perm_gate import (
    ALLOW,
    ASK,
    DENY,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
    build_advice_prompt,
    decide,
    decision_record,
    extract_subject,
    hook_output,
    map_risk,
    parse_verdict,
    run_hook,
    static_decision,
    strip_inert_strings,
)


def cfg(tmp, **over):
    """A PermGateConfig pointed at a temp logdir, guidelines deliberately absent
    (so read_guidelines -> '') unless a test overrides it."""
    env = {"REMOTE_CONTROL_LOGDIR": tmp,
           "PERM_GATE_GUIDELINES_FILE": str(Path(tmp) / "no-such.md")}
    env.update(over)
    return PermGateConfig.from_env(env)


class ConfigTest(unittest.TestCase):
    def test_defaults_shadow_and_thresholds(self):
        c = PermGateConfig.from_env({})
        self.assertFalse(c.enforce)          # shadow by default
        self.assertFalse(c.enforce_ai)       # static-only when enforce flips on
        self.assertTrue(c.enabled)
        self.assertTrue(c.consult_ai)
        self.assertIn("haiku", c.model)
        self.assertEqual((c.risk_ask_at, c.risk_deny_at), (ORANGE, RED))
        self.assertEqual(c.decisions_file.name, "perm-gate-decisions.jsonl")

    def test_threshold_overrides(self):
        c = PermGateConfig.from_env({"PERM_GATE_RISK_ASK_AT": "Yellow",
                                     "PERM_GATE_RISK_DENY_AT": "ORANGE"})
        self.assertEqual((c.risk_ask_at, c.risk_deny_at), (YELLOW, ORANGE))


class MapRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_default_mapping(self):
        c = cfg(self.tmp)
        self.assertEqual(map_risk(GREEN, c), ALLOW)
        self.assertEqual(map_risk(YELLOW, c), ALLOW)
        self.assertEqual(map_risk(ORANGE, c), ASK)
        self.assertEqual(map_risk(RED, c), DENY)

    def test_stricter_thresholds(self):
        c = cfg(self.tmp, PERM_GATE_RISK_ASK_AT="yellow", PERM_GATE_RISK_DENY_AT="orange")
        self.assertEqual(map_risk(GREEN, c), ALLOW)
        self.assertEqual(map_risk(YELLOW, c), ASK)
        self.assertEqual(map_risk(ORANGE, c), DENY)
        self.assertEqual(map_risk(RED, c), DENY)

    def test_unknown_tier_escalates_to_ask(self):
        self.assertEqual(map_risk("magenta", cfg(self.tmp)), ASK)


class ExtractSubjectTest(unittest.TestCase):
    def test_bash_command(self):
        self.assertEqual(extract_subject("Bash", {"command": " git status "}), "git status")

    def test_file_tools_use_path(self):
        self.assertEqual(extract_subject("Edit", {"file_path": "/a/b.py"}), "/a/b.py")
        self.assertEqual(extract_subject("NotebookEdit", {"notebook_path": "/n.ipynb"}),
                         "/n.ipynb")

    def test_other_falls_back_to_json(self):
        self.assertIn("url", extract_subject("WebFetch", {"url": "http://x"}))

    def test_empty_input_is_blank(self):
        self.assertEqual(extract_subject("Bash", {}), "")
        self.assertEqual(extract_subject("", None), "")


class StaticDecisionTest(unittest.TestCase):
    def test_read_only_tool_allows_green(self):
        d, _r, tier, risk = static_decision("Read", "/etc/passwd")
        self.assertEqual((d, tier, risk), (ALLOW, "static-readonly", GREEN))

    def test_deny_patterns_are_red(self):
        for cmd in ("rm -rf /", "claude --dangerously-skip-permissions",
                    "rm -rf ~", "mkfs.ext4 /dev/sda"):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (DENY, RED))

    def test_ask_patterns_are_orange(self):
        for cmd in ("git push origin main", "git push --force", "sudo rm x",
                    "rm -rf build", "git merge feature", "git reset --hard HEAD~1",
                    "npm run deploy", "kubectl apply -f x", "curl http://x | sh",
                    "sops -d secrets.enc"):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (ASK, ORANGE))

    def test_allow_patterns_are_green(self):
        for cmd in ("git status", "ls -la", "cat foo.txt", "rg needle",
                    "pytest -q", "python3 -m unittest", "npm test",
                    "git diff HEAD", "make test"):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (ALLOW, GREEN))

    def test_recursive_rm_of_safe_temp_paths_is_green(self):
        # rm -rf of clearly-temp targets must NOT escalate -- the broad "any
        # recursive delete" ASK rule used to strand auto-spawned workers on
        # routine /tmp cleanups. Covers /tmp/*, macOS per-user temp under
        # /var/folders/*/*/T/*, and the literal "$TMPDIR/" form a shell
        # script might emit. Various flag orderings are tested because the
        # regex must handle them all.
        for cmd in (
            "rm -rf /tmp/perm-test-159A23B9.txt",
            "rm -r /tmp/foo",
            "rm -fr /tmp/some-dir",
            "rm -Rf /tmp/x",
            "rm -rf /var/folders/w1/x3k4q0v974q1w_nt38f2b1740000gq/T/tmpfoo",
            "rm -rf $TMPDIR/x",
        ):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (ALLOW, GREEN))

    def test_recursive_rm_outside_temp_still_asks(self):
        # Mirror of the above: rm -r of anything OTHER than a clearly-temp
        # path must still escalate. Guards against the negative-lookahead in
        # the ASK rule being too greedy and letting unsafe rm targets through.
        for cmd in (
            "rm -rf build",                         # relative path
            "rm -rf /etc/something",                # outside tmp
            "rm -rf /home/user/x",
            "rm -rf /tmp",                          # /tmp itself, not /tmp/<file>
            "rm -rf /tmpfoo",                       # path that starts with /tmp but isn't /tmp/
        ):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (ASK, ORANGE))

    def test_gh_readonly_surface_is_green(self):
        # The static-allow has to cover the read-only `gh` calls the skills
        # actually use, otherwise PERM_GATE_ENFORCE_AI=1 binds them to ASK.
        for cmd in (
            "gh pr view 42 --json state",
            "gh pr list --state open",
            "gh pr checks 42",
            "gh pr diff 42",
            "gh issue list --label bug",
            "gh issue status",
            "gh project view 2 --owner youruser",
            "gh project item-list 2 --owner youruser --format json",
            "gh project field-list 2 --owner youruser",
            "gh repo view o/r --json defaultBranchRef",
            "gh release list --repo o/r",
            "gh workflow list",
            "gh run view 12345 --log",
            "gh gist list",
            "gh search issues --label bug",
            "gh search code 'TODO'",
            "gh label list --repo o/r",
            "gh secret list",
            "gh variable list",
            "gh auth status",
            "gh browse 42",
        ):
            with self.subTest(cmd=cmd):
                d, _r, _t, risk = static_decision("Bash", cmd)
                self.assertEqual((d, risk), (ALLOW, GREEN))

    def test_gh_mutating_verbs_fall_through_to_ai_tier(self):
        # Mutating gh subcommands must NOT static-allow -- they have to reach
        # the AI tier so ENFORCE_AI can bind them. (`gh api` is a wildcard for
        # any HTTP verb, so it also must not auto-allow.)
        for cmd in (
            "gh pr create --title x --body y",
            "gh pr merge 42 --squash",
            "gh pr close 42",
            "gh pr comment 42 --body hi",
            "gh issue create --title x",
            "gh issue edit 18 --add-label bug",
            "gh issue close 18",
            "gh project item-add 2 --owner u --url x",
            "gh project item-edit --id x --field-id y",
            "gh repo create o/r --public",
            "gh repo delete o/r --yes",
            "gh release create v1",
            "gh workflow run build.yml",
            "gh run rerun 12345",
            "gh run cancel 12345",
            "gh secret set FOO --body bar",
            "gh label create bug --color red",
            "gh api repos/o/r/issues -X POST",
            "gh api -X DELETE /repos/o/r/issues/1",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(static_decision("Bash", cmd))

    def test_chained_all_safe_allows(self):
        self.assertEqual(static_decision("Bash", "cd repo && git status && ls")[0], ALLOW)

    def test_chained_with_risky_segment_does_not_allow(self):
        self.assertEqual(static_decision("Bash", "ls && git push origin main")[0], ASK)

    def test_chained_with_unknown_segment_is_ambiguous(self):
        self.assertIsNone(static_decision("Bash", "ls && python3 mystery.py"))

    def test_unknown_bash_is_ambiguous(self):
        self.assertIsNone(static_decision("Bash", "python3 mystery.py --go"))

    def test_non_bash_mutating_tool_is_ambiguous(self):
        self.assertIsNone(static_decision("Edit", "/src/app.py"))

    def test_ask_ignores_prose_inside_double_quoted_body(self):
        # `--body` text that contains a trigger word must not bump the call
        # to ASK -- the gh action itself is benign.
        cmd = ('gh issue edit 18 --repo o/r --title "fix" '
               '--body "rotated .env files; sops -d secrets.enc"')
        self.assertIsNone(static_decision("Bash", cmd))

    def test_ask_ignores_prose_inside_commit_message(self):
        # The classic false-positive: `git commit -m "fix .env loading"`.
        self.assertIsNone(static_decision("Bash", 'git commit -m "fix .env loading"'))

    def test_ask_ignores_prose_inside_heredoc(self):
        cmd = (
            'gh issue create --body "$(cat <<\'EOF\'\n'
            'Notes: rotated .env, ran sops -d, redeploy via deploy.sh\n'
            'EOF\n'
            ')"'
        )
        self.assertIsNone(static_decision("Bash", cmd))

    def test_ask_still_fires_on_unquoted_trigger(self):
        # Same trigger word, but as a real command -- must still ASK.
        d, _r, _t, risk = static_decision("Bash", "git push origin main")
        self.assertEqual((d, risk), (ASK, ORANGE))

    def test_deny_still_sees_full_subject(self):
        # Stripping is ASK-only; DENY must still match dangerous content
        # even when it's hidden inside a quoted string -- we don't want the
        # paranoid layer fooled by ``echo "rm -rf /" | sh``.
        d, _r, _t, risk = static_decision("Bash", 'echo "rm -rf / # bomb"')
        self.assertEqual((d, risk), (DENY, RED))


class StripInertStringsTest(unittest.TestCase):
    def test_double_quoted_body_removed(self):
        self.assertNotIn(".env",
                         strip_inert_strings('git commit -m "fix .env loading"'))

    def test_single_quoted_body_removed(self):
        self.assertNotIn("sops",
                         strip_inert_strings("gh issue edit 1 --body 'ran sops -d'"))

    def test_heredoc_body_removed(self):
        s = strip_inert_strings(
            "cat <<EOF\nrotated .env files\nran sops -d\nEOF\n")
        self.assertNotIn(".env", s)
        self.assertNotIn("sops", s)

    def test_quoted_heredoc_marker_removed(self):
        s = strip_inert_strings(
            "cat <<'END'\ndeploy step\nEND\n")
        self.assertNotIn("deploy", s)

    def test_command_structure_preserved(self):
        s = strip_inert_strings('gh issue edit 18 --repo o/r --body "prose"')
        self.assertIn("gh issue edit 18", s)
        self.assertIn("--repo o/r", s)
        self.assertNotIn("prose", s)


class ParseVerdictTest(unittest.TestCase):
    def test_json_risk(self):
        self.assertEqual(parse_verdict('{"risk":"green","reason":"safe"}'),
                         ("", GREEN, "safe"))

    def test_json_legacy_decision(self):
        self.assertEqual(parse_verdict('{"decision":"allow","reason":"x"}'),
                         (ALLOW, "", "x"))

    def test_json_embedded_in_prose(self):
        _d, risk, _r = parse_verdict('Sure. {"risk":"red","reason":"force-push"} ok')
        self.assertEqual(risk, RED)

    def test_bare_risk_word(self):
        _d, risk, _r = parse_verdict("I'd call this orange, risky")
        self.assertEqual(risk, ORANGE)

    def test_bare_decision_word(self):
        d, risk, _r = parse_verdict("I would ASK the human")
        self.assertEqual((d, risk), (ASK, ""))

    def test_empty_and_garbage(self):
        self.assertEqual(parse_verdict("")[:2], ("", ""))
        self.assertEqual(parse_verdict("¯\\_(ツ)_/¯")[:2], ("", ""))


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_disabled_allows_everything(self):
        r = decide("Bash", {"command": "git push --force"},
                   cfg=cfg(self.tmp, PERM_GATE_ENABLED="0"))
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (ALLOW, "disabled", GREEN))

    def test_static_wins_without_advisor(self):
        r = decide("Bash", {"command": "git status"}, cfg=cfg(self.tmp),
                   advisor=lambda p: (DENY, RED, "should not be called"))
        self.assertEqual((r["decision"], r["tier"], r["risk"]),
                         (ALLOW, "static-allow", GREEN))

    def test_ambiguous_risk_maps_to_decision(self):
        seen = {}

        def adv(prompt):
            seen["prompt"] = prompt
            return ("", GREEN, "looks routine")

        r = decide("Bash", {"command": "python3 mystery.py"}, cfg=cfg(self.tmp),
                   guidelines="POLICY TEXT", advisor=adv)
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (ALLOW, "ai", GREEN))
        self.assertIn("mystery.py", seen["prompt"])
        self.assertIn("POLICY TEXT", seen["prompt"])

    def test_ai_red_denies(self):
        r = decide("Bash", {"command": "python3 exfil.py"}, cfg=cfg(self.tmp),
                   advisor=lambda p: ("", RED, "exfiltrates secrets"))
        self.assertEqual((r["decision"], r["risk"]), (DENY, RED))

    def test_ai_orange_asks(self):
        r = decide("Edit", {"file_path": "/x.py"}, cfg=cfg(self.tmp),
                   advisor=lambda p: ("", ORANGE, "edits prod config"))
        self.assertEqual((r["decision"], r["risk"]), (ASK, ORANGE))

    def test_ai_legacy_decision_backfills_risk(self):
        r = decide("Bash", {"command": "python3 mystery.py"}, cfg=cfg(self.tmp),
                   advisor=lambda p: (ALLOW, "", "fine"))
        self.assertEqual((r["decision"], r["risk"]), (ALLOW, GREEN))

    def test_ai_unclassifiable_escalates(self):
        r = decide("Bash", {"command": "python3 mystery.py"}, cfg=cfg(self.tmp),
                   advisor=lambda p: ("", "", "no idea"))
        self.assertEqual((r["decision"], r["risk"]), (ASK, ORANGE))

    def test_ai_respects_thresholds(self):
        # yellow normally allows, but with a stricter ask threshold it asks
        r = decide("Bash", {"command": "python3 mystery.py"},
                   cfg=cfg(self.tmp, PERM_GATE_RISK_ASK_AT="yellow"),
                   advisor=lambda p: ("", YELLOW, "minor"))
        self.assertEqual((r["decision"], r["risk"]), (ASK, YELLOW))

    def test_advisor_error_fails_safe(self):
        def boom(prompt):
            raise RuntimeError("network down")

        r = decide("Bash", {"command": "python3 mystery.py"}, cfg=cfg(self.tmp),
                   advisor=boom)
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (ASK, "ai-error", ORANGE))

    def test_consult_ai_off_asks_on_ambiguous(self):
        r = decide("Bash", {"command": "python3 mystery.py"},
                   cfg=cfg(self.tmp, PERM_GATE_CONSULT_AI="0"),
                   advisor=lambda p: ("", GREEN, "x"))
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (ASK, "no-rule", ORANGE))

    def test_empty_payload_never_hits_ai_and_asks(self):
        def boom(prompt):
            raise AssertionError("advisor must not be called for empty input")

        r = decide("Bash", {}, cfg=cfg(self.tmp), advisor=boom)
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (ASK, "no-subject", ORANGE))

    def test_static_only_enforce_skips_ai(self):
        # enforce on, enforce_ai off (default): ambiguous must NOT call the AI
        def boom(prompt):
            raise AssertionError("AI must not be consulted under static-only enforce")

        r = decide("Bash", {"command": "python3 mystery.py"},
                   cfg=cfg(self.tmp, PERM_GATE_ENFORCE="1"), advisor=boom)
        self.assertEqual((r["decision"], r["tier"]), (ASK, "no-rule"))

    def test_full_enforce_consults_ai(self):
        r = decide("Bash", {"command": "python3 mystery.py"},
                   cfg=cfg(self.tmp, PERM_GATE_ENFORCE="1", PERM_GATE_ENFORCE_AI="1"),
                   advisor=lambda p: ("", RED, "destructive"))
        self.assertEqual((r["decision"], r["tier"], r["risk"]), (DENY, "ai", RED))


class HookOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.result = {"decision": DENY, "reason": "nope", "tier": "static-deny",
                       "subject": "rm -rf /", "tool": "Bash", "risk": RED}

    def test_shadow_emits_nothing(self):
        self.assertIsNone(hook_output(self.result, cfg(self.tmp)))

    def test_static_only_enforce_binds_static(self):
        out = hook_output(self.result, cfg(self.tmp, PERM_GATE_ENFORCE="1"))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], DENY)
        self.assertEqual(hso["permissionDecisionReason"], "nope")

    def test_static_only_enforce_does_not_bind_ai(self):
        ai = {**self.result, "tier": "ai", "decision": DENY}
        self.assertIsNone(hook_output(ai, cfg(self.tmp, PERM_GATE_ENFORCE="1")))

    def test_full_enforce_binds_ai(self):
        ai = {**self.result, "tier": "ai", "decision": DENY}
        out = hook_output(ai, cfg(self.tmp, PERM_GATE_ENFORCE="1", PERM_GATE_ENFORCE_AI="1"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], DENY)


class RunHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _records(self, c):
        return [json.loads(l) for l in c.decisions_file.read_text().splitlines()]

    def test_shadow_logs_risk_but_emits_nothing(self):
        c = cfg(self.tmp)
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"},
                         "session_id": "cse_1"})
        out = run_hook(ev, c)
        self.assertIsNone(out)
        rec = self._records(c)[-1]
        self.assertEqual((rec["decision"], rec["risk"], rec["tier"]),
                         (ASK, ORANGE, "static-ask"))
        self.assertEqual(rec["session_id"], "cse_1")
        self.assertFalse(rec["enforced"])

    def test_enforce_emits_decision_and_logs_risk(self):
        c = cfg(self.tmp, PERM_GATE_ENFORCE="1")
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        out = run_hook(ev, c)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], ALLOW)
        rec = self._records(c)[-1]
        self.assertEqual(rec["risk"], GREEN)
        self.assertTrue(rec["enforced"])

    def test_ai_tier_via_injected_advisor(self):
        c = cfg(self.tmp)
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "python3 x.py"}})
        out = run_hook(ev, c, advisor=lambda p: ("", RED, "destructive"))
        self.assertIsNone(out)  # shadow
        rec = self._records(c)[-1]
        self.assertEqual((rec["tier"], rec["risk"], rec["decision"]), ("ai", RED, DENY))

    def test_malformed_stdin_does_not_crash(self):
        c = cfg(self.tmp)
        out = run_hook("this is not json", c)
        self.assertIsNone(out)
        self.assertEqual(len(self._records(c)), 1)

    def test_recursion_guard_emits_nothing_and_skips_logging(self):
        c = cfg(self.tmp)
        ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        out = run_hook(ev, c, env={"PERM_GATE_IN_ADVISOR": "1"})
        self.assertIsNone(out)
        self.assertFalse(c.decisions_file.exists())


class DecisionRecordTest(unittest.TestCase):
    def test_includes_risk_truncates_subject_marks_enforced(self):
        tmp = tempfile.mkdtemp()
        result = {"decision": ASK, "reason": "r", "tier": "ai",
                  "subject": "x" * 999, "tool": "Bash", "risk": ORANGE}
        rec = decision_record(result, {"session_id": "s", "cwd": "/c"},
                              cfg(tmp, PERM_GATE_ENFORCE="1"))
        self.assertEqual(len(rec["subject"]), 500)
        self.assertEqual((rec["session_id"], rec["risk"]), ("s", ORANGE))
        self.assertTrue(rec["enforced"])


class PromptTest(unittest.TestCase):
    def test_prompt_includes_call_policy_and_risk_scale(self):
        p = build_advice_prompt("Bash", "do thing", "MY POLICY")
        self.assertIn("do thing", p)
        self.assertIn("MY POLICY", p)
        self.assertIn("green|yellow|orange|red", p)
        self.assertIn("RISK", p)

    def test_prompt_without_policy(self):
        p = build_advice_prompt("Bash", "do thing", "")
        self.assertIn("do thing", p)
        self.assertNotIn("POLICY --", p)


if __name__ == "__main__":
    unittest.main()
