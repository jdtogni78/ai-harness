import os
import unittest

from pathlib import Path

from remote_control.session_titles import (
    apply_prefix,
    build_nickname_map,
    build_projects_index,
    build_worktree_index,
    derive_nickname,
    encode_dev_prefix,
    existing_prefix_host,
    extract_sub_token,
    extract_sub_tokens,
    is_host_local,
    live_session_entries,
    merged_repo_index,
    nickname_for,
    parse_cmd_session_id,
    parse_format_line,
    parse_nickname_map,
    plan_renames,
    render_template,
    repo_basename_from_url,
    repo_for_session,
    repo_from_cwd,
    repo_from_worktree_path,
    repo_sid_from_project_dirname,
    session_id_from_path,
    short_session_id,
    strip_prefix,
    title_format,
)


class NicknameMapTest(unittest.TestCase):
    def test_parse_strips_comments_and_blanks(self):
        text = "SampleRepo=SR\n# a comment\n\nfoo = BAR  # inline\n"
        self.assertEqual(parse_nickname_map(text), {"samplerepo": "SR", "foo": "BAR"})

    def test_build_precedence_env_over_file_over_builtin(self):
        # `dev` is in DEFAULT_NICKNAMES, so both file and env can override it;
        # this verifies env wins over file (which both win over the built-in).
        nmap = build_nickname_map("dev=FILE", "dev=ENV")
        self.assertEqual(nmap["dev"], "ENV")                # env wins
        self.assertEqual(nmap["claude-remote-control"], "CRC")  # built-in survives

    def test_lookup_is_case_insensitive(self):
        # Synthetic map: verify the lookup itself folds case, independent of
        # whatever the production NICKNAME_RULES happen to contain.
        nmap = {"sampleapp": "SAM"}
        self.assertEqual(nickname_for("SampleApp", nmap), "SAM")
        self.assertEqual(nickname_for("SAMPLEAPP", nmap), "SAM")


class DeriveNicknameTest(unittest.TestCase):
    def test_multiword_initials(self):
        self.assertEqual(derive_nickname("claude-remote-control"), "CRC")
        self.assertEqual(derive_nickname("job-search"), "JS")

    def test_camelcase_initials(self):
        self.assertEqual(derive_nickname("AppOne"), "AO")

    def test_single_word_first_three(self):
        self.assertEqual(derive_nickname("apptwo"), "APP")

    def test_unmapped_repo_falls_back_to_derived(self):
        self.assertEqual(nickname_for("SomeNewRepo", {}), "SNR")


class PrefixTest(unittest.TestCase):
    def test_strip_only_own_prefix(self):
        self.assertEqual(strip_prefix("[AO] Run tests"), "Run tests")
        self.assertEqual(strip_prefix("Run tests"), "Run tests")

    def test_apply_is_idempotent(self):
        once = apply_prefix("Run tests", "AO")
        self.assertEqual(once, "[AO] Run tests")
        self.assertEqual(apply_prefix(once, "AO"), "[AO] Run tests")

    def test_apply_replaces_old_prefix(self):
        self.assertEqual(apply_prefix("[claude-remote-control] x", "CRC"), "[CRC] x")

    def test_does_not_strip_long_bracket_prose(self):
        # Beyond the 64-char prefix window (widened to fit {id}/{branch} tokens),
        # a human's long bracketed sentence is left untouched.
        title = "[this is a long bracketed sentence that we should not be touching at all here] x"
        self.assertEqual(strip_prefix(title), title)

    def test_strips_host_branch_prefix_idempotently(self):
        # A composite prefix we emit ([nick.host.branch]) must round-trip so the
        # self-heal pass replaces rather than stacks it.
        self.assertEqual(apply_prefix("[CRC.mini.feature-x] do y", "CRC.note"),
                         "[CRC.note] do y")

    def test_strips_prefix_with_any_separator(self):
        # User templates can use separators outside [\w] (e.g. @, /, :); the strip
        # must still recognize our prefix so re-runs don't stack.
        for old in ("[repo@mini] t", "[a/b:c] t", "[claude-remote-control@mini] t"):
            self.assertEqual(apply_prefix(old, "X"), "[X] t")

    def test_strip_chained_prefix(self):
        # `[NICK][SUB] desc` strips both brackets when there's no space between
        # them -- the form new-session uses for tagged subsessions.
        self.assertEqual(strip_prefix("[CRC.mini][deadbeef] do x"), "do x")

    def test_extract_sub_token(self):
        self.assertEqual(extract_sub_token("[CRC.mini][deadbeef] do x"), "deadbeef")
        # Single-bracket title -> no SUB token to extract.
        self.assertIsNone(extract_sub_token("[CRC.mini] do x"))
        # No prefix at all.
        self.assertIsNone(extract_sub_token("plain title"))

    def test_apply_prefix_with_sub_emits_chained_form(self):
        self.assertEqual(apply_prefix("do x", "CRC.mini", sub="deadbeef"),
                         "[CRC.mini][deadbeef] do x")

    def test_apply_prefix_with_sub_round_trips(self):
        # Re-applying with the same SUB on an already-chained title must replace,
        # not stack -- this is exactly the watcher's behavior on every pass.
        once = apply_prefix("do x", "CRC.mini", sub="deadbeef")
        twice = apply_prefix(once, "CRC.note", sub="deadbeef")
        self.assertEqual(twice, "[CRC.note][deadbeef] do x")

    def test_existing_prefix_host_reads_first_bracket_only(self):
        # A [NICK.host][SUB] chained title still resolves to the host claim
        # (the host segment lives in the FIRST bracket; SUB is a sibling).
        self.assertEqual(existing_prefix_host("[CRC.mini][SUB] x"), "mini")

    # ------------------------------------------------------------------
    # Duplicated-prefix self-healing (issue #69).
    #
    # The platform's auto-titler can race the watcher's PUT and leave the
    # title in a space-separated bracket form. The watcher's own
    # apply_prefix then stacks ``[NICK] `` on top, producing
    # ``[NICK] [NICK][SUB] body``. Before the fix, strip_prefix consumed
    # only the first ``[NICK] ``, extract_sub_token returned None, and
    # apply_prefix was idempotent on the dup -- watcher considered it
    # "correct" and never PUT a fix. These tests lock in the tolerance.
    # ------------------------------------------------------------------
    def test_strip_dup_prefix_form(self):
        # The two-bracket dup form the issue surfaced: watcher must strip
        # both bracket-groups (with the inter-bracket space) so the body
        # round-trips to the unprefixed form.
        self.assertEqual(
            strip_prefix("[FF.mini] [FF.mini][MGR-1] Managing 1 worker"),
            "Managing 1 worker")

    def test_strip_triple_dup_prefix_form(self):
        # Multi-pass accumulation: every watcher tick that doesn't strip
        # the carry-over stacks one more [NICK] on top.
        self.assertEqual(
            strip_prefix("[AH.mini] [AH.mini] [AH.mini][mgr-board14] auto-spawned"),
            "auto-spawned")

    def test_strip_space_separated_two_brackets_without_sub(self):
        # Edge: a title that genuinely begins with two unrelated bracketed
        # tokens separated by a space. The strip is still applied (we can't
        # distinguish this from a dup at this layer); apply_prefix replaces
        # them with the canonical [NICK] form. This trades a (rare) human
        # title quirk for the (common) self-heal pass working.
        self.assertEqual(strip_prefix("[A] [B] body"), "body")

    def test_strip_does_not_eat_bracket_without_separator(self):
        # ``[A][B]nospace`` -- no whitespace between the bracket-run and
        # the body -- must be left alone, same as today's behavior. The
        # lookbehind guard makes this hold even when the regex iterates
        # multiple bracket-groups.
        title = "[A][B]nospace"
        self.assertEqual(strip_prefix(title), title)

    def test_strip_leaves_bare_title_unchanged(self):
        self.assertEqual(strip_prefix("body"), "body")
        self.assertEqual(strip_prefix(""), "")

    def test_extract_sub_from_dup_form(self):
        # 3 leading brackets: first two are the dup of [NICK]; the last
        # is the SUB that new-session set at spawn time. The watcher
        # re-emits the chained form against the canonical [NICK].
        self.assertEqual(
            extract_sub_token("[FF.mini] [FF.mini][MGR-1] Managing 1 worker"),
            "MGR-1")

    def test_extract_sub_from_triple_dup_form(self):
        # 4 leading brackets: pick the last as the SUB.
        self.assertEqual(
            extract_sub_token("[AH.mini] [AH.mini] [AH.mini][mgr-board14] body"),
            "mgr-board14")

    def test_extract_sub_from_clean_chained_form(self):
        # The non-dup case still works (this is the test_extract_sub_token
        # path; duplicated here as an explicit baseline for the edge tests).
        self.assertEqual(extract_sub_token("[CRC.mini][deadbeef] body"), "deadbeef")

    def test_extract_sub_from_space_separated_two_brackets(self):
        # Edge: ``[A] [B] body`` -- two leading brackets, space between
        # them. With only 2 brackets the heuristic can't tell "A is a
        # stacked dup of NICK, B is the SUB" apart from "A is one tag, B
        # is another tag". We treat the LAST as the SUB regardless, so
        # the self-heal path covers the dup case; the cost is that a
        # human-written ``[A] [B] body`` re-renders to ``[NICK][B] body``
        # on the watcher's next pass. Documented heuristic, not a bug.
        self.assertEqual(extract_sub_token("[A] [B] body"), "B")

    def test_extract_sub_none_for_single_bracket(self):
        self.assertIsNone(extract_sub_token("[CRC.mini] body"))
        self.assertIsNone(extract_sub_token("[A] body"))

    def test_extract_sub_none_for_bare_title(self):
        self.assertIsNone(extract_sub_token("body"))
        self.assertIsNone(extract_sub_token(""))
        self.assertIsNone(extract_sub_token(None))

    def test_extract_sub_none_when_brackets_not_followed_by_space(self):
        # Mirror of test_strip_does_not_eat_bracket_without_separator:
        # extract_sub_token and strip_prefix must agree on what counts as
        # a leading prefix. Otherwise plan_renames would drop the [B]
        # body on a title that strip_prefix leaves untouched.
        self.assertIsNone(extract_sub_token("[A][B]nospace"))

    def test_extract_sub_tokens_multi_with_nick(self):
        # With nick supplied, every leading bracket equal to nick is dropped
        # (the dup-NICK heal case); the remainder is the real sub chain.
        self.assertEqual(
            extract_sub_tokens("[DEV.mini][MGR-1][S1] body", nick="DEV.mini"),
            ["MGR-1", "S1"])
        self.assertEqual(
            extract_sub_tokens("[DEV.mini] [DEV.mini][MGR-1][S1] body",
                               nick="DEV.mini"),
            ["MGR-1", "S1"])
        self.assertEqual(
            extract_sub_tokens("[DEV.mini] body", nick="DEV.mini"), [])

    def test_apply_prefix_with_subs_emits_chain(self):
        # `subs=` (multi) appends one bracket per element after the NICK; an
        # empty entry collapses out.
        self.assertEqual(
            apply_prefix("body", "DEV.mini", subs=["MGR-1", "S1"]),
            "[DEV.mini][MGR-1][S1] body")
        self.assertEqual(
            apply_prefix("body", "DEV.mini", subs=["MGR-1", "", "S1"]),
            "[DEV.mini][MGR-1][S1] body")
        # `subs=` overrides legacy single `sub=`.
        self.assertEqual(
            apply_prefix("body", "DEV.mini", sub="ignored", subs=["A", "B"]),
            "[DEV.mini][A][B] body")

    def test_plan_renames_preserves_multi_sub_chain(self):
        # The watcher's pass on a [NICK][MGR-N][S<k>] title must round-trip the
        # entire chain, not just the last bracket.
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini][MGR-2][S3] body",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, {"cse_bridge": "claude-remote-control"},
            build_nickname_map(), host="mini")}
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini][MGR-2][S3] body")
        self.assertFalse(plan["cse_bridge"].changed)

    def test_apply_prefix_heals_dup_form(self):
        # End-to-end: watcher's apply_prefix on the dup form must collapse
        # back to the canonical chained form. This is the round-trip the
        # whole fix is about -- without it, plan_renames produces a
        # changed=False rename and the dup persists.
        dup = "[FF.mini] [FF.mini][MGR-1] Managing 1 worker"
        sub = extract_sub_token(dup)
        self.assertEqual(apply_prefix(dup, "FF.mini", sub=sub),
                         "[FF.mini][MGR-1] Managing 1 worker")

    def test_apply_prefix_heals_triple_dup_form(self):
        dup = "[AH.mini] [AH.mini] [AH.mini][mgr-board14] auto-spawned"
        sub = extract_sub_token(dup)
        self.assertEqual(apply_prefix(dup, "AH.mini", sub=sub),
                         "[AH.mini][mgr-board14] auto-spawned")


class RepoDerivationTest(unittest.TestCase):
    def test_basename_from_url(self):
        self.assertEqual(
            repo_basename_from_url("https://github.com/me/AppOne.git"), "AppOne")
        self.assertEqual(
            repo_basename_from_url("https://github.com/me/AppOne"), "AppOne")

    def test_sources_url_wins(self):
        s = {"id": "cse_1", "config": {"sources": [
            {"url": "https://github.com/me/AppOne.git"}]}}
        self.assertEqual(repo_for_session(s, {"cse_1": "wrong"}), "AppOne")

    def test_bridge_falls_back_to_worktree_index(self):
        s = {"id": "cse_2", "config": {"sources": []}}
        self.assertEqual(repo_for_session(s, {"cse_2": "claude-remote-control"}),
                         "claude-remote-control")

    def test_undeterminable_repo_is_none(self):
        self.assertIsNone(repo_for_session({"id": "cse_3", "config": {}}, {}))


class HostLocalTest(unittest.TestCase):
    def test_bridge_session_in_index_is_local(self):
        s = {"id": "cse_b", "config": {}}
        self.assertTrue(is_host_local(s, {"cse_b": "ai-harness"}))

    def test_source_url_does_not_disqualify_local_bridge(self):
        # A sandbox session forked to a local bridge dir still runs HERE; the
        # local index entry wins over the config.sources[].url cloud signal.
        s = {"id": "cse_b", "config": {"sources": [{"url": "x"}]}}
        self.assertTrue(is_host_local(s, {"cse_b": "ai-harness"}))

    def test_source_url_without_local_bridge_is_not_local(self):
        s = {"id": "cse_b", "config": {"sources": [{"url": "x"}]}}
        self.assertFalse(is_host_local(s, {}))

    def test_unknown_session_is_not_local(self):
        self.assertFalse(is_host_local({"id": "cse_z", "config": {}}, {}))


class TemplateTest(unittest.TestCase):
    def test_all_tokens_present(self):
        vals = {"nick": "AO", "host": "mini", "branch": "main"}
        self.assertEqual(render_template("{nick}.{host}/{branch}", vals), "AO.mini/main")

    def test_empty_token_collapses_preceding_separator(self):
        self.assertEqual(render_template("{nick}.{host}", {"nick": "AO"}), "AO")

    def test_empty_token_collapses_following_separator(self):
        # Token first, empty -> drop the separator that follows it.
        self.assertEqual(render_template("{host}.{nick}", {"nick": "AO"}), "AO")

    def test_empty_middle_token_keeps_outer_pair(self):
        vals = {"nick": "AO", "branch": "main"}  # host empty
        self.assertEqual(render_template("{nick}.{host}.{branch}", vals), "AO.main")

    def test_unknown_token_is_empty(self):
        self.assertEqual(render_template("{nick}.{bogus}", {"nick": "AO"}), "AO")

    def test_literal_text_preserved(self):
        vals = {"nick": "AO", "host": "mini"}
        self.assertEqual(render_template("{nick} @{host}", vals), "AO @mini")


class TitleFormatTest(unittest.TestCase):
    def test_parse_format_line_last_wins(self):
        text = "SampleRepo=SR\nformat={nick}\n# c\nformat = {nick}.{host}\n"
        self.assertEqual(parse_format_line(text), "{nick}.{host}")

    def test_parse_format_line_absent(self):
        self.assertIsNone(parse_format_line("SampleRepo=SR\n"))

    def test_format_key_not_a_nickname(self):
        # The reserved `format` key must not leak into the repo->nick map.
        self.assertNotIn("format", parse_nickname_map("format={nick}.{host}\n"))

    def test_precedence_env_over_file_over_default(self):
        self.assertEqual(title_format("format={nick}", "{repo}"), "{repo}")  # env wins
        self.assertEqual(title_format("format={repo}", ""), "{repo}")        # file
        self.assertEqual(title_format("", ""), "{nick}.{host}")              # default

    def test_short_session_id(self):
        self.assertEqual(short_session_id("cse_01XtFhCtBTr4fEzBmXVUdFgj"), "01XtFhCt")
        self.assertEqual(short_session_id("abcd"), "abcd")


class SelfPathTest(unittest.TestCase):
    WT = "/Users/me/dev/ai-harness/.claude/worktrees/bridge-cse_01ABC"

    def test_session_id_from_worktree_path(self):
        self.assertEqual(session_id_from_path(self.WT), "cse_01ABC")

    def test_session_id_from_subdir_of_worktree(self):
        self.assertEqual(session_id_from_path(self.WT + "/remote_control"), "cse_01ABC")

    def test_session_id_none_outside_worktree(self):
        self.assertIsNone(session_id_from_path("/Users/me/dev/ai-harness"))

    def test_repo_from_worktree_path(self):
        self.assertEqual(repo_from_worktree_path(self.WT), "ai-harness")

    def test_repo_none_outside_worktree(self):
        self.assertIsNone(repo_from_worktree_path("/tmp/bridge-cse_x"))


class RunSetRepoFallbackTest(unittest.TestCase):
    """`titles set` must recover the repo (hence the [NICK] prefix) from the
    session's git source URL when neither the cwd nor the local worktree index
    resolves it -- the cloud / non-`.claude` worktree case that previously got
    renamed verbatim, with no prefix."""

    def _run(self, opts, sessions):
        from remote_control import session_titles as st

        captured = {}

        def fake_set(cfg, token, sid, title):
            captured["sid"], captured["title"] = sid, title
            return 200, {}

        orig_list = st.monitor.list_sessions
        orig_set = st.set_title
        st.monitor.list_sessions = lambda cfg, token, log: sessions
        st.set_title = fake_set
        try:
            base = {"dev": "/nonexistent", "file": "/nonexistent", "map": "",
                    "self": False, "id": None, "desc": "",
                    "projects": "/nonexistent"}
            base.update(opts)
            rc = st._run_set(None, "tok", base, log=lambda m: None)
        finally:
            st.monitor.list_sessions = orig_list
            st.set_title = orig_set
        return rc, captured

    def test_id_cloud_session_prefixed_from_url(self):
        cloud = {"id": "cse_cloud", "config": {"sources": [
            {"url": "https://github.com/me/AppOne.git"}]}}
        rc, cap = self._run({"id": "cse_cloud", "desc": "#7 do thing"}, [cloud])
        self.assertEqual(rc, 0)
        self.assertEqual(cap["title"], "[AO] #7 do thing")

    def test_set_without_repo_keeps_description_verbatim(self):
        orphan = {"id": "cse_x", "config": {}}
        rc, cap = self._run({"id": "cse_x", "desc": "no repo here"}, [orphan])
        self.assertEqual(rc, 0)
        self.assertEqual(cap["title"], "no repo here")

    def test_force_host_adds_suffix_even_when_indexer_blind(self):
        """`set --id <sid> --force-host` adds the `.host` suffix to a session
        whose bridge worktree the local indexer can't see. The escape hatch
        for sessions running on this host with a worktree outside the scanned
        dev roots -- without it, ``host_local = sid in index`` is False and
        the prefix template collapses ``{host}`` to empty (-> ``[AO]`` with no
        host segment). Paired with the plan_renames self-claim guard, this
        keeps the monitor's title-pass from stripping the suffix on the next
        tick."""
        cloud = {"id": "cse_orphan", "config": {"sources": [
            {"url": "https://github.com/me/AppOne.git"}]}}
        prev_host = os.environ.get("REMOTE_CONTROL_HOST")
        os.environ["REMOTE_CONTROL_HOST"] = "note"
        try:
            rc, cap = self._run(
                {"id": "cse_orphan", "desc": "self-claim", "force_host": True},
                [cloud])
        finally:
            if prev_host is None:
                os.environ.pop("REMOTE_CONTROL_HOST", None)
            else:
                os.environ["REMOTE_CONTROL_HOST"] = prev_host
        self.assertEqual(rc, 0)
        self.assertEqual(cap["title"], "[AO.note] self-claim")

    def test_id_without_force_host_omits_suffix_when_indexer_blind(self):
        """Baseline for the test above: same setup minus ``--force-host``
        produces ``[AO]`` (no host segment) because is_host_local is False.
        Documents the pre-existing behavior the flag opts out of."""
        cloud = {"id": "cse_orphan", "config": {"sources": [
            {"url": "https://github.com/me/AppOne.git"}]}}
        prev_host = os.environ.get("REMOTE_CONTROL_HOST")
        os.environ["REMOTE_CONTROL_HOST"] = "note"
        try:
            rc, cap = self._run(
                {"id": "cse_orphan", "desc": "no claim"},
                [cloud])
        finally:
            if prev_host is None:
                os.environ.pop("REMOTE_CONTROL_HOST", None)
            else:
                os.environ["REMOTE_CONTROL_HOST"] = prev_host
        self.assertEqual(rc, 0)
        self.assertEqual(cap["title"], "[AO] no claim")

    def test_cwd_override_recovers_repo_outside_index(self):
        """`titles set --id <sid> --cwd <dir>` derives [NICK] from the dir
        when neither the on-disk index nor the API source URL knows the sid.
        This is the path the manage skill uses to tag a manager session that
        isn't running inside any bridge worktree."""
        from remote_control import session_titles as st

        captured = {}

        def fake_set(cfg, token, sid, title):
            captured["title"] = title
            return 200, {}

        orig_repo_cwd, orig_list, orig_set = (
            st.repo_from_cwd, st.monitor.list_sessions, st.set_title)
        prev_host = os.environ.get("REMOTE_CONTROL_HOST")
        st.repo_from_cwd = lambda cwd, dev: "AppOne"
        st.monitor.list_sessions = lambda cfg, token, log: []
        st.set_title = fake_set
        os.environ["REMOTE_CONTROL_HOST"] = "mini"
        try:
            opts = {"dev": "/nonexistent", "file": "/nonexistent", "map": "",
                    "self": False, "id": "cse_mgr",
                    "desc": "public scans (2 workers)",
                    "projects": "/nonexistent", "cwd": "/some/dir",
                    "subs": ["MGR-1"]}
            rc = st._run_set(None, "tok", opts, log=lambda m: None)
        finally:
            st.repo_from_cwd, st.monitor.list_sessions, st.set_title = (
                orig_repo_cwd, orig_list, orig_set)
            if prev_host is None:
                os.environ.pop("REMOTE_CONTROL_HOST", None)
            else:
                os.environ["REMOTE_CONTROL_HOST"] = prev_host
        self.assertEqual(rc, 0)
        self.assertEqual(captured["title"],
                         "[AO.mini][MGR-1] public scans (2 workers)")

    def test_self_local_bridge_gets_host_suffix(self):
        """`set --self` is run from inside a session's own worktree on this host,
        so it's host-local -> the prefix carries the `.host` suffix ([AO.mini])."""
        from remote_control import session_titles as st

        captured = {}

        def fake_set(cfg, token, sid, title):
            captured["title"] = title
            return 200, {}

        orig_sid, orig_repo, orig_set = (
            st.session_id_from_path, st.repo_from_worktree_path, st.set_title)
        prev_host = os.environ.get("REMOTE_CONTROL_HOST")
        st.session_id_from_path = lambda p: "cse_self"
        st.repo_from_worktree_path = lambda p: "AppOne"
        st.set_title = fake_set
        os.environ["REMOTE_CONTROL_HOST"] = "mini"
        try:
            opts = {"dev": "/nonexistent", "file": "/nonexistent", "map": "",
                    "self": True, "id": None, "desc": "do thing",
                    "projects": "/nonexistent"}
            rc = st._run_set(None, "tok", opts, log=lambda m: None)
        finally:
            st.session_id_from_path, st.repo_from_worktree_path, st.set_title = (
                orig_sid, orig_repo, orig_set)
            if prev_host is None:
                os.environ.pop("REMOTE_CONTROL_HOST", None)
            else:
                os.environ["REMOTE_CONTROL_HOST"] = prev_host
        self.assertEqual(rc, 0)
        self.assertEqual(captured["title"], "[AO.mini] do thing")


class PlanRenamesTest(unittest.TestCase):
    def setUp(self):
        self.nmap = build_nickname_map()
        self.index = {"cse_bridge": "claude-remote-control"}
        self.sessions = [
            {"id": "cse_cloud", "title": "Add feature",
             "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}},
            {"id": "cse_bridge", "title": "[CRC] Already prefixed", "config": {}},
            {"id": "cse_orphan", "title": "No repo", "config": {}},
        ]

    def test_cloud_session_gets_prefix(self):
        plan = {r.id: r for r in plan_renames(self.sessions, self.index, self.nmap)}
        self.assertEqual(plan["cse_cloud"].new_title, "[AO] Add feature")
        self.assertTrue(plan["cse_cloud"].changed)

    def test_already_prefixed_is_unchanged(self):
        plan = {r.id: r for r in plan_renames(self.sessions, self.index, self.nmap)}
        self.assertFalse(plan["cse_bridge"].changed)

    def test_unknown_repo_left_alone(self):
        plan = {r.id: r for r in plan_renames(self.sessions, self.index, self.nmap)}
        self.assertIsNone(plan["cse_orphan"].repo)
        self.assertFalse(plan["cse_orphan"].changed)

    def test_host_suffix_on_local_bridge_session_only(self):
        # With a host, the local bridge session (in the index, no source URL) gets
        # a `.host` suffix; the cloud session (source URL) stays host-less.
        plan = {r.id: r for r in
                plan_renames(self.sessions, self.index, self.nmap, host="mini")}
        self.assertEqual(plan["cse_bridge"].new_title, "[CRC.mini] Already prefixed")
        self.assertTrue(plan["cse_bridge"].changed)
        self.assertEqual(plan["cse_cloud"].new_title, "[AO] Add feature")

    def test_no_host_suffix_when_host_omitted(self):
        # Backward-compatible default: no host -> the bare repo nickname.
        plan = {r.id: r for r in plan_renames(self.sessions, self.index, self.nmap)}
        self.assertEqual(plan["cse_cloud"].new_title, "[AO] Add feature")
        self.assertFalse(plan["cse_bridge"].changed)

    def test_branch_token_resolved_for_local_session_only(self):
        called = []

        def branch_for(sid, repo):
            called.append((sid, repo))
            return "feature-x"

        plan = {r.id: r for r in plan_renames(
            self.sessions, self.index, self.nmap, host="mini",
            template="{nick}.{host}.{branch}", branch_for=branch_for)}
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini.feature-x] Already prefixed")
        self.assertEqual(plan["cse_cloud"].new_title, "[AO] Add feature")  # host/branch collapse
        self.assertEqual(called, [("cse_bridge", "claude-remote-control")])  # cloud not consulted

    def test_branch_resolver_not_called_when_token_absent(self):
        called = []
        plan_renames(self.sessions, self.index, self.nmap, host="mini",
                     template="{nick}.{host}", branch_for=lambda s, r: called.append(s) or "x")
        self.assertEqual(called, [])  # no {branch} in template -> zero git calls

    def test_sub_token_preserved_across_pass(self):
        # A subname tag set by new-session ([CRC.mini][deadbeef]) must survive
        # the watcher's re-render -- otherwise the tag dies on the next ~10min
        # pass and the picker loses the subsession marker. plan_renames reads
        # the SUB out of the old title and re-emits it.
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini][deadbeef] auto-spawned",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, self.index, self.nmap, host="mini")}
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini][deadbeef] auto-spawned")
        # No change -> watcher won't issue a PUT (good: less API churn).
        self.assertFalse(plan["cse_bridge"].changed)

    def test_sub_token_preserved_when_outer_token_changes(self):
        # Outer prefix needs updating (host changed mini -> note); the SUB tag
        # must survive that rewrite. Without preservation, the watcher would
        # drop [deadbeef] silently on the first cross-host pass.
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini][deadbeef] auto-spawned",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, self.index, self.nmap, host="note")}
        # mini-claim was foreign, so the title is left alone (existing_prefix_host
        # check). This is the right behavior: another host owns the claim.
        self.assertFalse(plan["cse_bridge"].changed)

    def test_sub_token_preserved_when_only_desc_changed(self):
        # Same host, same outer token, only the human-supplied desc differs.
        # The watcher's pass should re-emit the SUB tag against the new desc.
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini][deadbeef] cloud auto-renamed this",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, self.index, self.nmap, host="mini")}
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini][deadbeef] cloud auto-renamed this")
        self.assertFalse(plan["cse_bridge"].changed)

    def test_plan_self_heals_dup_prefix_form(self):
        # Issue #69 regression: a title that has accumulated the dup form
        # ``[CRC.mini] [CRC.mini][SUB] body`` must round-trip to the
        # canonical chained form on the next pass. Before the fix
        # extract_sub_token returned None and apply_prefix was idempotent
        # on the dup -> changed=False -> watcher never PUT a fix.
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini] [CRC.mini][deadbeef] body",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, self.index, self.nmap, host="mini")}
        self.assertTrue(plan["cse_bridge"].changed,
                        f"watcher must self-heal dup form: {plan['cse_bridge']}")
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini][deadbeef] body")

    def test_plan_self_heals_triple_dup_prefix_form(self):
        sessions = [{"id": "cse_bridge",
                     "title": "[CRC.mini] [CRC.mini] [CRC.mini][deadbeef] body",
                     "config": {}}]
        plan = {r.id: r for r in plan_renames(
            sessions, self.index, self.nmap, host="mini")}
        self.assertTrue(plan["cse_bridge"].changed)
        self.assertEqual(plan["cse_bridge"].new_title,
                         "[CRC.mini][deadbeef] body")

    def test_id_and_shortid_tokens_render_for_any_session(self):
        plan = {r.id: r for r in plan_renames(
            self.sessions, self.index, self.nmap, template="{nick}-{shortid}")}
        self.assertEqual(plan["cse_cloud"].new_title, "[AO-cloud] Add feature")
        self.assertEqual(plan["cse_bridge"].new_title, "[CRC-bridge] Already prefixed")

    def test_note_pass_does_not_overwrite_mini_claim(self):
        """Mirror of test_mini_pass_does_not_overwrite_note_claim. The guard
        must be symmetric: note's pass leaves ``[*.mini]`` alone."""
        sessions = [{"id": "cse_x", "title": "[AO.mini] keep mini", "config": {}}]
        index = {"cse_x": "AppOne"}
        plan = plan_renames(sessions, index, self.nmap, host="note")
        self.assertFalse(plan[0].changed)
        self.assertEqual(plan[0].new_title, "[AO.mini] keep mini")

    def test_mini_pass_does_not_overwrite_note_claim(self):
        """The reverse direction: mini's pass leaves ``[*.note]`` alone, even
        when mini has the bridge worktree in its local index (which would
        otherwise re-render the prefix as ``[AO.mini]``)."""
        sessions = [{"id": "cse_y", "title": "[AO.note] keep note", "config": {}}]
        index = {"cse_y": "AppOne"}
        plan = plan_renames(sessions, index, self.nmap, host="mini")
        self.assertFalse(plan[0].changed)
        self.assertEqual(plan[0].new_title, "[AO.note] keep note")

    def test_other_host_claim_left_alone(self):
        """A title like ``[AO.mini] x`` is left untouched by note's pass even
        when note can derive the repo (mm-log tail mentions the sid). Without
        this guard, the two hosts' self-heal passes would ping-pong the
        suffix every cycle."""
        sessions = [
            # cloud-style session (source URL set) but already labeled `.mini`;
            # note's pass should leave it alone.
            {"id": "cse_other", "title": "[AO.mini] keep mini",
             "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}},
            # same shape but labeled with THIS host: re-prefix is fine
            # (idempotent self-heal of our own claim).
            {"id": "cse_ours", "title": "[AO.note] our claim",
             "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}},
            # no host segment in the existing prefix -> still re-prefixed (we're
            # adding a fresh claim, not overwriting one).
            {"id": "cse_hostless", "title": "[AO] hostless",
             "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}},
        ]
        # note has AO in its local index (bridge worktree present locally) so the
        # `is_host_local` -> ``.note`` rendering fires for sids without a foreign
        # host claim. The mini-claimed sid is still left alone.
        index = {"cse_other": "AppOne",
                 "cse_ours": "AppOne",
                 "cse_hostless": "AppOne"}
        plan = {r.id: r for r in plan_renames(sessions, index, self.nmap, host="note")}
        self.assertFalse(plan["cse_other"].changed, plan["cse_other"])
        self.assertEqual(plan["cse_other"].new_title, "[AO.mini] keep mini")
        # Our own claim and the host-less title both re-render with note's
        # suffix: a local bridge dir means the session physically runs here,
        # even though config.sources has a github URL attached.
        self.assertEqual(plan["cse_ours"].new_title, "[AO.note] our claim")
        self.assertEqual(plan["cse_hostless"].new_title, "[AO.note] hostless")

    def test_own_host_claim_preserved_when_not_in_worktree_index(self):
        """The bug-fix case: a session this host owns by title (``[AO.note]``)
        but is NOT in our local worktree index (e.g. bridge worktree lives
        outside the scanned roots) must keep its host suffix.

        Before the fix, ``is_host_local`` returned False, ``host_local=False``
        collapsed ``{host}`` to empty in the template, and ``apply_prefix``
        rewrote ``[AO.note] x`` -> ``[AO] x`` -- the same self-overwrite the
        cross-host guard explicitly prevents for foreign claims. After the
        fix a matching self-claim is authoritative: it implies host-local even
        when the on-disk indexer can't see the session."""
        sessions = [
            {"id": "cse_orphan", "title": "[AO.note] outside-index",
             "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}},
        ]
        # Empty worktree index: the session id is NOT a known local bridge
        # here, so is_host_local returns False. repo_for_session still
        # derives a repo from config.sources -- the rename can proceed.
        plan = {r.id: r for r in plan_renames(
            sessions, {}, self.nmap, host="note")}
        self.assertFalse(plan["cse_orphan"].changed,
                         f"own-host claim must survive: {plan['cse_orphan']}")
        self.assertEqual(plan["cse_orphan"].new_title, "[AO.note] outside-index")


class ExistingPrefixHostTest(unittest.TestCase):
    def test_extracts_host_from_nick_dot_host(self):
        self.assertEqual(existing_prefix_host("[AO.mini] x"), "mini")
        self.assertEqual(existing_prefix_host("[AO.note] x"), "note")

    def test_none_for_single_segment_prefix(self):
        self.assertIsNone(existing_prefix_host("[AH] x"))
        self.assertIsNone(existing_prefix_host("[CRC] doing stuff"))

    def test_none_for_no_prefix(self):
        self.assertIsNone(existing_prefix_host("plain title"))
        self.assertIsNone(existing_prefix_host(""))

    def test_handles_alternate_separators(self):
        # The template can render with `/`, `@`, `:`, or whitespace too.
        self.assertEqual(existing_prefix_host("[AO/mini] x"), "mini")
        self.assertEqual(existing_prefix_host("[AO@mini] x"), "mini")
        self.assertEqual(existing_prefix_host("[AO mini] x"), "mini")

    def test_last_segment_wins_for_three_token_template(self):
        # Templates like {nick}.{host}.{branch} put branch last, which the
        # current implementation treats as "host". That's a known limitation;
        # the rule is "last segment != my host -> leave alone", which is the
        # safe-conservative direction (we'd skip rather than overwrite).
        self.assertEqual(existing_prefix_host("[AO.mini.main] x"), "main")


class ProjectsIndexTest(unittest.TestCase):
    """The projects-index source: derive {sid: repo} from the on-disk transcript
    folder names under ~/.claude/projects, so sessions whose bridge worktree was
    deleted (but whose transcript folder survives) are still renameable."""

    def test_encode_dev_prefix(self):
        # "/Users/me/dev" -> "-Users-me-dev-"; the trailing "-" anchors the prefix
        # check to a path-segment boundary.
        self.assertEqual(encode_dev_prefix("/Users/me/dev"), "-Users-me-dev-")
        # "." collapses to "-" too (so the encoded ~ /.claude becomes --claude).
        self.assertEqual(encode_dev_prefix("/srv/.foo"), "-srv--foo-")

    def test_parse_simple_repo(self):
        name = "-Users-user-dev-ai-harness--claude-worktrees-bridge-cse-01ABC"
        self.assertEqual(
            repo_sid_from_project_dirname(name, "-Users-user-dev-"),
            ("ai-harness", "cse_01ABC"),
        )

    def test_parse_hyphenated_repo(self):
        # A repo name that itself contains `-` (`app-two-docker`,
        # `claude-remote-control`) must round-trip because the suffix we split on
        # is unambiguous.
        for repo in ("app-two-docker", "claude-remote-control"):
            name = f"-Users-user-dev-{repo}--claude-worktrees-bridge-cse-01X"
            self.assertEqual(
                repo_sid_from_project_dirname(name, "-Users-user-dev-"),
                (repo, "cse_01X"),
            )

    def test_parse_rejects_non_bridge_dir(self):
        self.assertIsNone(repo_sid_from_project_dirname(
            "-Users-user-dev", "-Users-user-dev-"))
        self.assertIsNone(repo_sid_from_project_dirname(
            "-Users-user-dev-ai-harness", "-Users-user-dev-"))

    def test_parse_rejects_mismatched_dev_prefix(self):
        # A transcript dir from a different user / dev root must not slip into
        # this host's index.
        name = "-Users-other-dev-AppOne--claude-worktrees-bridge-cse-01X"
        self.assertIsNone(repo_sid_from_project_dirname(
            name, "-Users-user-dev-"))

    def test_parse_rejects_empty_sid_or_repo(self):
        # Defensive: an empty repo segment or empty sid segment shouldn't yield
        # a partial mapping.
        self.assertIsNone(repo_sid_from_project_dirname(
            "-Users-user-dev---claude-worktrees-bridge-cse-01X",
            "-Users-user-dev-"))
        self.assertIsNone(repo_sid_from_project_dirname(
            "-Users-user-dev-ai-harness--claude-worktrees-bridge-cse-",
            "-Users-user-dev-"))

    def test_build_projects_index_scans_dir(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A real bridge transcript dir, a non-bridge sibling, and a file.
            (root / "-Users-me-dev-ai-harness--claude-worktrees-bridge-cse-01A").mkdir()
            (root / "-Users-me-dev-AppOne--claude-worktrees-bridge-cse-02B").mkdir()
            (root / "-Users-me-dev").mkdir()  # ignored: no bridge suffix
            (root / "stray.jsonl").write_text("x")
            idx = build_projects_index(root, "/Users/me/dev")
            self.assertEqual(idx, {"cse_01A": "ai-harness", "cse_02B": "AppOne"})

    def test_build_projects_index_missing_dir_returns_empty(self):
        from pathlib import Path
        self.assertEqual(
            build_projects_index(Path("/nonexistent-projects-dir"), "/Users/me/dev"),
            {},
        )

    def test_merged_repo_index_worktree_wins_on_conflict(self):
        import tempfile
        from pathlib import Path
        from remote_control import session_titles as st

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Same sid in both sources but disagreeing repos. The worktree-index
            # is the live truth, so it wins.
            (root / "-Users-me-dev-stale-repo--claude-worktrees-bridge-cse-01X").mkdir()
            orig_wt = st.build_worktree_index
            st.build_worktree_index = lambda dev: {"cse_01X": "live-repo"}
            try:
                idx = merged_repo_index("/Users/me/dev", str(root))
            finally:
                st.build_worktree_index = orig_wt
            self.assertEqual(idx["cse_01X"], "live-repo")

    def test_merged_repo_index_projects_fills_gaps(self):
        import tempfile
        from pathlib import Path
        from remote_control import session_titles as st

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "-Users-me-dev-app-two--claude-worktrees-bridge-cse-02Y").mkdir()
            orig_wt = st.build_worktree_index
            st.build_worktree_index = lambda dev: {}  # no live worktree
            try:
                idx = merged_repo_index("/Users/me/dev", str(root))
            finally:
                st.build_worktree_index = orig_wt
            self.assertEqual(idx, {"cse_02Y": "app-two"})

    def test_build_mm_log_index_extracts_cse_from_session_link(self):
        import tempfile
        from pathlib import Path
        from remote_control.session_titles import build_mm_log_index

        with tempfile.TemporaryDirectory() as tmp:
            logdir = Path(tmp)
            (logdir / "mm-dev.log").write_text(
                "noise\nsession_01ABCxyz?from=cliAttached\nmore noise\n"
            )
            (logdir / "mm-AppOne.log").write_text(
                "session_02DEFxyz?from=cliAO launch readiness\n"
            )
            (logdir / "manager.log").write_text(
                "not an mm- file: session_03GHIjkl?from=cli\n"
            )
            idx = build_mm_log_index(logdir, "mm")
            self.assertEqual(
                idx, {"cse_01ABCxyz": "dev", "cse_02DEFxyz": "AppOne"})

    def test_build_mm_log_index_reads_only_tail(self):
        """Stale cse-ids past the tail window drop out, so a session that has
        moved to another host stops being claimed by this one."""
        import tempfile
        from pathlib import Path
        from remote_control import session_titles as st
        from remote_control.session_titles import build_mm_log_index

        with tempfile.TemporaryDirectory() as tmp:
            logdir = Path(tmp)
            stale = "session_STALEold?from=cli\n"
            recent = "session_FRESHnew?from=cli\n"
            # Pad so the stale id falls outside the tail window.
            padding = b"x" * (st.MM_LOG_TAIL_BYTES + 1024)
            (logdir / "mm-dev.log").write_bytes(
                stale.encode() + padding + recent.encode())
            idx = build_mm_log_index(logdir, "mm")
            self.assertEqual(idx, {"cse_FRESHnew": "dev"})

    def test_build_mm_log_index_missing_dir_returns_empty(self):
        from pathlib import Path
        from remote_control.session_titles import build_mm_log_index
        self.assertEqual(build_mm_log_index(Path("/nonexistent-logdir"), "mm"), {})

    def test_merged_repo_index_mm_log_fills_gaps_lowest_precedence(self):
        import tempfile
        from pathlib import Path
        from remote_control import session_titles as st

        with tempfile.TemporaryDirectory() as wt_tmp, \
             tempfile.TemporaryDirectory() as log_tmp:
            # mm-log names a different repo for the same sid; worktree wins.
            (Path(log_tmp) / "mm-dev.log").write_text(
                "session_01X?from=cli\nsession_03Z?from=cli\n")
            orig_wt = st.build_worktree_index
            st.build_worktree_index = lambda dev: {"cse_01X": "AppOne"}
            try:
                idx = merged_repo_index("/Users/me/dev", None, logdir=log_tmp, host="mm")
            finally:
                st.build_worktree_index = orig_wt
            # Worktree's repo for cse_01X wins over mm-log's "dev"; cse_03Z is
            # only in mm-log, so it gets filled with "dev".
            self.assertEqual(idx, {"cse_01X": "AppOne", "cse_03Z": "dev"})


class ApplyPrefixesTest(unittest.TestCase):
    """`apply_prefixes` (the monitor's self-heal pass) re-prefixes every session
    whose repo is derivable, skips correct/undeterminable ones, and returns
    (renamed_ok, failed)."""

    def _run(self, sessions, set_codes=None):
        from remote_control import session_titles as st

        calls = []
        codes = dict(set_codes or {})

        def fake_set(cfg, token, sid, title):
            calls.append((sid, title))
            return codes.get(sid, 200), {}

        orig_list, orig_set = st.monitor.list_sessions, st.set_title
        st.monitor.list_sessions = lambda cfg, token, log: sessions
        st.set_title = fake_set
        try:
            res = st.apply_prefixes(None, "tok", lambda m: None,
                                    dev="/nonexistent", file="/nonexistent",
                                    projects="/nonexistent")
        finally:
            st.monitor.list_sessions, st.set_title = orig_list, orig_set
        return res, calls

    def _ff(self, sid, title):
        return {"id": sid, "title": title,
                "config": {"sources": [{"url": "https://github.com/me/AppOne.git"}]}}

    def test_prefixes_derivable_and_skips_correct_and_orphan(self):
        sessions = [
            self._ff("cse_a", "do x"),
            self._ff("cse_b", "[AO] already"),
            {"id": "cse_orphan", "title": "no repo", "config": {}},
        ]
        (ok, fail), calls = self._run(sessions)
        self.assertEqual((ok, fail), (1, 0))
        self.assertEqual(calls, [("cse_a", "[AO] do x")])

    def test_counts_failures(self):
        (ok, fail), _ = self._run([self._ff("cse_a", "do x")],
                                  set_codes={"cse_a": 500})
        self.assertEqual((ok, fail), (0, 1))

    def test_none_sessions_is_noop(self):
        from remote_control import session_titles as st
        orig = st.monitor.list_sessions
        st.monitor.list_sessions = lambda cfg, token, log: None
        try:
            self.assertEqual(
                st.apply_prefixes(None, "tok", lambda m: None), (0, 0))
        finally:
            st.monitor.list_sessions = orig


class ParseCmdSessionIdTest(unittest.TestCase):
    def test_extracts_from_argv_slice(self):
        cmd = "/path/to/claude --print --session-id cse_01ABCxyz --foo"
        self.assertEqual(parse_cmd_session_id(cmd), "cse_01ABCxyz")

    def test_none_when_arg_missing(self):
        self.assertIsNone(parse_cmd_session_id("/path/to/claude --print"))
        self.assertIsNone(parse_cmd_session_id(""))
        self.assertIsNone(parse_cmd_session_id(None))

    def test_extracts_from_jwt_env_var(self):
        # JWT payload {"session_id":"cse_fromjwt"} -- header/sig are stand-ins
        # (the parser ignores them; only the middle segment matters).
        import base64
        payload = base64.urlsafe_b64encode(
            b'{"session_id":"cse_fromjwt"}').rstrip(b"=").decode()
        jwt = f"eyJhbGciOiJFUzI1NiJ9.{payload}.eyJzaWcifQ"
        blob = f"claude --print CLAUDE_CODE_SESSION_ACCESS_TOKEN=sk-ant-si-{jwt} FOO=bar"
        self.assertEqual(parse_cmd_session_id(blob), "cse_fromjwt")

    def test_argv_arg_wins_over_jwt(self):
        # If both are present, the explicit --session-id arg should win
        # (the JWT may belong to a parent process's env).
        import base64
        payload = base64.urlsafe_b64encode(
            b'{"session_id":"cse_jwt"}').rstrip(b"=").decode()
        jwt = f"eyJhbGciOiJFUzI1NiJ9.{payload}.eyJzaWcifQ"
        blob = f"claude --session-id cse_argv --print TOKEN=sk-ant-si-{jwt}"
        self.assertEqual(parse_cmd_session_id(blob), "cse_argv")

    def test_ignores_garbled_jwt(self):
        # A JWT-shaped string with non-base64 payload should not crash or match.
        self.assertIsNone(parse_cmd_session_id(
            "claude eyJabc.notb64@@@.eyJsig FOO=bar"))


class RepoFromCwdTest(unittest.TestCase):
    def test_repo_root_matches(self):
        self.assertEqual(repo_from_cwd("/d/AppOne", "/d"), "AppOne")

    def test_subdir_picks_top_level_repo(self):
        self.assertEqual(
            repo_from_cwd("/d/AppOne/.claude/worktrees/bridge-cse_x", "/d"),
            "AppOne")
        self.assertEqual(repo_from_cwd("/d/app-two/app/web", "/d"), "app-two")

    def test_outside_dev_returns_none(self):
        self.assertIsNone(repo_from_cwd("/elsewhere/repo", "/d"))

    def test_equal_to_dev_returns_none(self):
        self.assertIsNone(repo_from_cwd("/d", "/d"))


class LiveSessionEntriesTest(unittest.TestCase):
    def test_maps_cse_to_repo_via_pid_cmd(self):
        records = [
            {"pid": 100, "cwd": "/d/AppOne"},
            {"pid": 200, "cwd": "/d/app-two/sub"},
            {"pid": 300, "cwd": "/elsewhere"},          # outside dev
            {"pid": 400, "cwd": "/d/dev"},              # cmd has no --session-id
        ]
        cmds = {100: "claude --session-id cse_a --print",
                200: "claude --print --session-id cse_b",
                300: "claude --session-id cse_c --print",
                400: "claude --print"}
        idx = live_session_entries(
            Path("/ignored"), "/d",
            cmd_for=cmds.get,
            read_records=lambda _p: records)
        self.assertEqual(idx, {"cse_a": "AppOne", "cse_b": "app-two"})

    def test_skips_unreadable_dir(self):
        # missing dir => read_records returns []
        self.assertEqual(
            live_session_entries(Path("/nope"), "/d",
                                 cmd_for=lambda _: "",
                                 read_records=lambda _p: []),
            {})

    def test_setdefault_first_writer_wins(self):
        records = [
            {"pid": 100, "cwd": "/d/A"},
            {"pid": 101, "cwd": "/d/B"},
        ]
        cmds = {100: "claude --session-id cse_x",
                101: "claude --session-id cse_x"}  # same cse from two pids
        idx = live_session_entries(
            Path("/ignored"), "/d",
            cmd_for=cmds.get,
            read_records=lambda _p: records)
        self.assertEqual(idx, {"cse_x": "A"})


class BuildWorktreeIndexLiveMergeTest(unittest.TestCase):
    def test_bridge_wins_over_live_session(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dev:
            d = Path(dev)
            (d / "AppOne" / ".claude" / "worktrees" / "bridge-cse_dup").mkdir(parents=True)
            records = [{"pid": 100, "cwd": str(d / "OtherRepo")}]
            cmds = {100: "claude --session-id cse_dup"}
            idx = build_worktree_index(
                d, sessions_dir=Path("/ignored"),
                cmd_for=cmds.get, read_records=lambda _p: records)
            # bridge entry wins (AppOne), even though live points elsewhere
            self.assertEqual(idx.get("cse_dup"), "AppOne")

    def test_live_session_extends_index_for_non_bridge(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dev:
            d = Path(dev)
            (d / "ai-harness").mkdir()  # repo root, no bridge dir
            records = [{"pid": 200, "cwd": str(d / "ai-harness")}]
            cmds = {200: "claude --session-id cse_root --print"}
            idx = build_worktree_index(
                d, sessions_dir=Path("/ignored"),
                cmd_for=cmds.get, read_records=lambda _p: records)
            self.assertEqual(idx.get("cse_root"), "ai-harness")


class TitlesWatchDaemonTest(unittest.TestCase):
    """The ``titles watch`` daemon loop: separated from the usage-limit
    monitor so the two services have independent cadences, lockfiles, and
    restart triggers. Each tick re-reads the OAuth token (so a launchd
    respawn or token rotation propagates without restarting the daemon)
    and best-effort calls ``apply_prefixes``; an exception in there must
    not break the loop."""

    def _cfg(self, tmp):
        import os
        prev = os.environ.get("REMOTE_CONTROL_LOGDIR")
        os.environ["REMOTE_CONTROL_LOGDIR"] = tmp
        try:
            from remote_control.config import UsageLimitConfig
            return UsageLimitConfig.from_env()
        finally:
            if prev is None:
                os.environ.pop("REMOTE_CONTROL_LOGDIR", None)
            else:
                os.environ["REMOTE_CONTROL_LOGDIR"] = prev

    def test_calls_apply_prefixes_on_each_tick(self):
        """Big clock values (matching production wall-clock) so the
        ``now - last >= interval`` check fires on the first outer tick.
        Use a small interval so the inner sleep-loop is short enough that
        we get multiple outer iterations before the stop fires."""
        from remote_control import session_titles as st
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            calls = []
            orig_apply, orig_token = st.apply_prefixes, st.monitor.get_token
            st.apply_prefixes = lambda c, t, log: calls.append(t) or (0, 0)
            st.monitor.get_token = lambda c, log: "tok"
            # interval=1 → inner loop sleeps just once between outer iterations.
            # Clocks 1000, 1001, 1002 → both pass the >= 1 threshold.
            ticks = iter([1000.0, 1001.0, 1002.0])
            def clock():
                try:
                    return next(ticks)
                except StopIteration:
                    raise KeyboardInterrupt  # bound the loop
            try:
                st._run_watch(cfg, lambda m: None, interval=1,
                              sleep=lambda _s: None, clock=clock)
            except KeyboardInterrupt:
                pass
            finally:
                st.apply_prefixes, st.monitor.get_token = orig_apply, orig_token
            # 3 clock reads → 3 outer iterations → 3 apply_prefixes calls
            # (all token reads pass the threshold).
            self.assertGreaterEqual(len(calls), 2,
                                    f"expected >=2 apply_prefixes calls, got {len(calls)}")

    def test_lockfile_blocks_second_instance(self):
        from remote_control import session_titles as st
        import os, tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            lock = st._titles_watcher_lock_file(cfg)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()))  # OUR pid -> "live"
            logs = []
            rc = st._run_watch(cfg, logs.append, interval=999,
                               sleep=lambda _: None, clock=lambda: 0.0)
            self.assertEqual(rc, 0)
            self.assertTrue(any("another instance running" in m for m in logs),
                            f"expected refusal log; got {logs}")
            # Lockfile preserved for the live owner.
            self.assertTrue(lock.exists())
            lock.unlink()  # cleanup

    def test_apply_exception_does_not_break_loop(self):
        """A failure inside apply_prefixes must be logged and swallowed so
        the daemon survives transient API hiccups."""
        from remote_control import session_titles as st
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            orig_apply, orig_token = st.apply_prefixes, st.monitor.get_token
            calls = [0]
            def boom(c, t, log):
                calls[0] += 1
                raise RuntimeError("simulated api 500")
            st.apply_prefixes = boom
            st.monitor.get_token = lambda c, log: "tok"
            logs = []
            ticks = iter([1000.0, 1001.0, 1002.0])
            def clock():
                try:
                    return next(ticks)
                except StopIteration:
                    raise KeyboardInterrupt
            try:
                st._run_watch(cfg, logs.append, interval=1,
                              sleep=lambda _: None, clock=clock)
            except KeyboardInterrupt:
                pass
            finally:
                st.apply_prefixes, st.monitor.get_token = orig_apply, orig_token
            # Multiple calls means the loop kept ticking after the first raise.
            self.assertGreaterEqual(calls[0], 2,
                                    f"loop should survive exceptions; got {calls[0]} calls")
            self.assertTrue(any("apply pass failed" in m for m in logs),
                            f"expected failure log; got {logs}")


class TitlesWatchArgParseTest(unittest.TestCase):
    def test_watch_subcommand_recognized(self):
        from remote_control.session_titles import _parse_args
        opts = _parse_args(["watch"])
        self.assertEqual(opts["cmd"], "watch")
        self.assertEqual(opts["interval"], 0)   # default; resolved at runtime

    def test_interval_flag_parsed(self):
        from remote_control.session_titles import _parse_args
        opts = _parse_args(["watch", "--interval", "120"])
        self.assertEqual(opts["cmd"], "watch")
        self.assertEqual(opts["interval"], 120)


if __name__ == "__main__":
    unittest.main()
