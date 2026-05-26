import unittest
from pathlib import Path

from remote_control.discovery import (
    Server,
    discover,
    host_allows,
    load_allowlist,
    nickname_from_hostname,
)

ANY = "anyhost"  # host used when entries are unscoped; must not matter


class AllowlistTest(unittest.TestCase):
    def test_strips_comments_blanks_whitespace(self):
        text = "# header\n\ndev\nAppOne   # inline\n  app-two \n\n"
        self.assertEqual(
            load_allowlist(text),
            {"dev": None, "AppOne": None, "app-two": None},
        )

    def test_empty_or_comment_only_yields_empty(self):
        self.assertEqual(load_allowlist(""), {})
        self.assertEqual(load_allowlist("# only comment\n\n"), {})

    def test_host_scoped_entry_parsed(self):
        self.assertEqual(
            load_allowlist("AppOne@user\n"),
            {"AppOne": frozenset({"user"})},
        )

    def test_multiple_hosts_and_normalization(self):
        self.assertEqual(
            load_allowlist("app-two @ User, User2 \n"),
            {"app-two": frozenset({"user", "user2"})},
        )

    def test_repeat_entries_union_hosts(self):
        self.assertEqual(
            load_allowlist("AppOne@a\nAppOne@b\n"),
            {"AppOne": frozenset({"a", "b"})},
        )

    def test_bare_entry_wins_over_scoped(self):
        # An unscoped line means "any host" regardless of a scoped sibling.
        self.assertEqual(load_allowlist("AppOne@a\nAppOne\n"), {"AppOne": None})
        self.assertEqual(load_allowlist("AppOne\nAppOne@a\n"), {"AppOne": None})

    def test_trailing_at_means_any(self):
        self.assertEqual(load_allowlist("AppOne@\n"), {"AppOne": None})


class HostAllowsTest(unittest.TestCase):
    def test_none_allows_any(self):
        self.assertTrue(host_allows(None, "whatever"))

    def test_membership_case_insensitive(self):
        self.assertTrue(host_allows(frozenset({"user"}), "User"))
        self.assertFalse(host_allows(frozenset({"user"}), "user2"))


class NicknameTest(unittest.TestCase):
    def test_macbook_rule(self):
        self.assertEqual(nickname_from_hostname("User2s-MacBook-Pro-2.local"), "note")
        self.assertEqual(nickname_from_hostname("work-macbook-air"), "note")

    def test_macmini_rule(self):
        self.assertEqual(nickname_from_hostname("macmini.local"), "mini")
        self.assertEqual(nickname_from_hostname("Claudios-MacMini.lan"), "mini")

    def test_unmatched_falls_back_to_first_label_lowercased(self):
        self.assertEqual(nickname_from_hostname("Some-Server.lan"), "some-server")
        self.assertEqual(nickname_from_hostname("USER"), "user")


class DiscoverTest(unittest.TestCase):
    def setUp(self):
        self.dev = Path("/Users/user/dev")
        self.subdirs = [self.dev / "AppOne", self.dev / "app-two", self.dev / "notallowed"]

    def test_root_emitted_same_dir_when_allowed(self):
        self.assertEqual(
            discover(self.dev, {"dev": None}, [], lambda d: True, ANY),
            [Server("mm-dev", self.dev, "same-dir")],
        )

    def test_root_skipped_when_not_allowed(self):
        servers = discover(self.dev, {"AppOne": None}, self.subdirs, lambda d: True, ANY)
        self.assertNotIn("mm-dev", [s.name for s in servers])

    def test_subdir_worktree_when_git_usable(self):
        self.assertEqual(
            discover(self.dev, {"AppOne": None}, self.subdirs, lambda d: True, ANY),
            [Server("mm-AppOne", self.dev / "AppOne", "worktree")],
        )

    def test_subdir_same_dir_when_git_unusable(self):
        self.assertEqual(
            discover(self.dev, {"app-two": None}, self.subdirs, lambda d: False, ANY),
            [Server("mm-app-two", self.dev / "app-two", "same-dir")],
        )

    def test_only_allowlisted_subdirs(self):
        names = {s.name for s in discover(
            self.dev, {"AppOne": None, "app-two": None}, self.subdirs, lambda d: False, ANY)}
        self.assertEqual(names, {"mm-AppOne", "mm-app-two"})

    def test_no_double_emit_when_subdir_collides_with_root(self):
        subs = [self.dev / "dev"]  # a subdir literally named like the root base
        self.assertEqual(
            discover(self.dev, {"dev": None}, subs, lambda d: True, ANY),
            [Server("mm-dev", self.dev, "same-dir")],
        )

    def test_git_probe_only_called_for_allowlisted(self):
        called = []

        def probe(d):
            called.append(d)
            return True

        discover(self.dev, {"AppOne": None}, self.subdirs, probe, ANY)
        self.assertEqual(called, [self.dev / "AppOne"])

    # --- host scoping ---
    def test_scoped_subdir_spawns_on_matching_host(self):
        allow = {"AppOne": frozenset({"user"})}
        names = {s.name for s in discover(self.dev, allow, self.subdirs, lambda d: False, "user")}
        self.assertEqual(names, {"mm-AppOne"})

    def test_scoped_subdir_skipped_on_other_host(self):
        allow = {"AppOne": frozenset({"user"})}
        self.assertEqual(discover(self.dev, allow, self.subdirs, lambda d: False, "user2"), [])

    def test_scoped_subdir_skipped_does_not_probe_git(self):
        called = []
        allow = {"AppOne": frozenset({"user"})}
        discover(self.dev, allow, self.subdirs, lambda d: called.append(d) or True, "user2")
        self.assertEqual(called, [])

    def test_scoped_root(self):
        self.assertEqual(
            discover(self.dev, {"dev": frozenset({"user2"})}, [], lambda d: True, "user"),
            [],
        )
        self.assertEqual(
            discover(self.dev, {"dev": frozenset({"user"})}, [], lambda d: True, "user"),
            [Server("mm-dev", self.dev, "same-dir")],
        )


if __name__ == "__main__":
    unittest.main()
