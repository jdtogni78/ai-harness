import io
import unittest
from pathlib import Path
from unittest import mock

from remote_control.session_fork_all import (
    IdResult,
    all_ok,
    fork_and_archive_one,
    summarize,
)


def _result(cse_id="cse_a", **kw) -> IdResult:
    defaults = dict(cse_id=cse_id, new_sid="new", fork_ok=True,
                    fork_err="", archived=None, archive_err="")
    defaults.update(kw)
    return IdResult(**defaults)


class SummarizeTest(unittest.TestCase):
    def test_fork_only(self):
        rs = [_result(cse_id="a"), _result(cse_id="b", fork_ok=False)]
        self.assertEqual(summarize(rs), "1/2 forked")

    def test_with_archive(self):
        rs = [_result(cse_id="a", archived=True),
              _result(cse_id="b", archived=False, archive_err="503"),
              _result(cse_id="c", archived=True)]
        out = summarize(rs)
        self.assertIn("3/3 forked", out)
        self.assertIn("2/3 archived", out)
        self.assertIn("1 archive failures", out)


class AllOkTest(unittest.TestCase):
    def test_all_forked(self):
        self.assertTrue(all_ok([_result()], require_archive=False))

    def test_one_fork_fails(self):
        self.assertFalse(all_ok([_result(), _result(fork_ok=False)],
                                require_archive=False))

    def test_require_archive(self):
        rs = [_result(archived=True), _result(archived=False)]
        self.assertFalse(all_ok(rs, require_archive=True))
        self.assertTrue(all_ok([_result(archived=True),
                                _result(archived=True)], require_archive=True))

    def test_require_archive_but_not_attempted(self):
        # archived=None means we asked for archive but never even attempted
        # (e.g. token fetch failed) -- still a failure under require_archive.
        self.assertFalse(all_ok([_result(archived=None)],
                                require_archive=True))


class ForkAndArchiveOneTest(unittest.TestCase):
    """Drive fork_and_archive_one with do_fork and monitor.archive_session
    monkeypatched. The function's contract is sequencing -- it must NEVER
    call archive after a failed fork, and NEVER call archive on dry run."""

    def _make_dest(self, exists=True) -> Path:
        # A fake destination path whose .exists() the caller controls.
        p = mock.Mock(spec=Path)
        p.exists.return_value = exists
        p.__str__ = lambda self: "/fake/dest.jsonl"
        return p

    def _fake_fork_result(self, dest):
        # Mimics session_fork.ForkResult shape -- only the fields fork_and_archive_one
        # reads (new_sid, dest).
        r = mock.Mock()
        r.new_sid = "new-uuid"
        r.dest = dest
        return r

    def test_dry_run_never_archives(self):
        with mock.patch("remote_control.session_fork_all.do_fork",
                        return_value=self._fake_fork_result(self._make_dest())) as do_fork, \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=False,
                cfg=mock.Mock(), token="tok", log=lambda m: None,
            )
        do_fork.assert_called_once()
        monitor.archive_session.assert_not_called()
        self.assertTrue(r.fork_ok)
        self.assertIsNone(r.archived)

    def test_fork_failure_skips_archive(self):
        with mock.patch("remote_control.session_fork_all.do_fork",
                        side_effect=FileNotFoundError("nope")) as do_fork, \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=True,
                cfg=mock.Mock(), token="tok", log=lambda m: None,
            )
        self.assertFalse(r.fork_ok)
        self.assertIn("nope", r.fork_err)
        monitor.archive_session.assert_not_called()

    def test_archive_only_when_dest_exists(self):
        # If do_fork says success but the file isn't on disk, treat as failure.
        with mock.patch("remote_control.session_fork_all.do_fork",
                        return_value=self._fake_fork_result(self._make_dest(exists=False))), \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=True,
                cfg=mock.Mock(), token="tok", log=lambda m: None,
            )
        self.assertFalse(r.fork_ok)
        monitor.archive_session.assert_not_called()

    def test_archive_runs_after_successful_fork(self):
        with mock.patch("remote_control.session_fork_all.do_fork",
                        return_value=self._fake_fork_result(self._make_dest())), \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            monitor.archive_session.return_value = (200, {})
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=True,
                cfg=mock.Mock(), token="tok", log=lambda m: None,
            )
        monitor.archive_session.assert_called_once()
        self.assertTrue(r.fork_ok)
        self.assertIs(r.archived, True)

    def test_archive_failure_does_not_invalidate_fork(self):
        with mock.patch("remote_control.session_fork_all.do_fork",
                        return_value=self._fake_fork_result(self._make_dest())), \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            monitor.archive_session.return_value = (503, "down")
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=True,
                cfg=mock.Mock(), token="tok", log=lambda m: None,
            )
        self.assertTrue(r.fork_ok)  # fork still counts
        self.assertIs(r.archived, False)
        self.assertIn("503", r.archive_err)

    def test_archive_skipped_without_token(self):
        # If the OAuth token couldn't be fetched, fork still runs; archive is
        # noted as not-done with a clear reason rather than crashing.
        with mock.patch("remote_control.session_fork_all.do_fork",
                        return_value=self._fake_fork_result(self._make_dest())), \
             mock.patch("remote_control.session_fork_all.monitor") as monitor:
            r = fork_and_archive_one(
                "cse_x", projects_root=Path("/"), dev="/dev",
                into_main=True, archive=True, write=True,
                cfg=mock.Mock(), token=None, log=lambda m: None,
            )
        monitor.archive_session.assert_not_called()
        self.assertTrue(r.fork_ok)
        self.assertIs(r.archived, False)
        self.assertIn("no OAuth token", r.archive_err)


if __name__ == "__main__":
    unittest.main()
