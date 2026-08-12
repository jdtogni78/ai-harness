"""Unit tests for the Telegram inbound bridge (#163).

Makes NO live authenticated calls (cf #114): the Bot API client is exercised
only through a fake ``urlopen`` / injected ``run``, and every test uses a MOCK
token or none at all. No network, no real token, no real feed writes.
"""
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote_control.config import TelegramConfig
from remote_control.telegram import bridge


def _cfg(tmp: Path, **over) -> TelegramConfig:
    env = {
        "HOME": str(tmp),
        "REMOTE_CONTROL_LOGDIR": str(tmp / "logs"),
        "TELEGRAM_TOKEN_FILE": str(tmp / "token"),
        "TELEGRAM_ANSWERS_SCRIPT": str(tmp / "answers.sh"),
    }
    env.update(over)
    return TelegramConfig.from_env(env)


def _log_sink():
    lines = []
    return lines, (lambda m: lines.append(m))


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = TelegramConfig.from_env({"HOME": "/tmp/x"})
        self.assertEqual(c.api_base, "https://api.telegram.org")
        self.assertEqual(c.token_file, Path("/tmp/x/.ai-harness/telegram/bot_token"))
        self.assertTrue(c.dry_run)  # default ON
        self.assertEqual(c.allowed_chat_ids, frozenset())
        self.assertTrue(str(c.answers_script).endswith("skills/manage/scripts/answers.sh"))

    def test_state_paths_under_logdir(self):
        c = TelegramConfig.from_env({"HOME": "/tmp/x", "REMOTE_CONTROL_LOGDIR": "/l"})
        self.assertEqual(c.state_file, Path("/l/telegram-bridge-state.json"))
        self.assertEqual(c.lock_file, Path("/l/telegram-bridge.lock"))
        self.assertEqual(c.log_file, Path("/l/telegram-bridge.log"))

    def test_allowlist_parsed(self):
        c = TelegramConfig.from_env(
            {"HOME": "/tmp/x", "TELEGRAM_ALLOWED_CHAT_IDS": "111, 222 ,"})
        self.assertEqual(c.allowed_chat_ids, frozenset({"111", "222"}))

    def test_dry_run_off(self):
        c = TelegramConfig.from_env({"HOME": "/tmp/x", "TELEGRAM_DRY_RUN": "0"})
        self.assertFalse(c.dry_run)


class SanitizeTest(unittest.TestCase):
    def test_strips_control_chars_and_nul(self):
        self.assertEqual(bridge.sanitize_text("a\x00b\x07c", 100), "abc")

    def test_keeps_tab_and_newline(self):
        self.assertEqual(bridge.sanitize_text("a\tb\nc", 100), "a\tb\nc")

    def test_strips_c1_controls(self):
        self.assertEqual(bridge.sanitize_text("a\x85b", 100), "ab")

    def test_caps_length_with_marker(self):
        out = bridge.sanitize_text("x" * 50, 10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_non_str_coerced(self):
        self.assertEqual(bridge.sanitize_text(12345, 100), "12345")
        self.assertEqual(bridge.sanitize_text(None, 100), "")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(bridge.sanitize_text("  hi  ", 100), "hi")


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.cfg = TelegramConfig.from_env({"HOME": "/tmp/x"})

    def _update(self, **msg):
        base = {"update_id": 7, "message": {"text": "hello",
                "chat": {"id": 42}, "from": {"id": 9, "username": "boss"},
                "date": 1000}}
        base["message"].update(msg)
        return base

    def test_basic(self):
        m = bridge.normalize_message(self._update(), self.cfg)
        self.assertEqual(m, {"update_id": 7, "chat_id": 42, "sender": "boss",
                             "sender_id": 9, "text": "hello", "date": 1000})

    def test_sanitizes_text(self):
        m = bridge.normalize_message(self._update(text="hi\x00\x07there"), self.cfg)
        self.assertEqual(m["text"], "hithere")

    def test_falls_back_to_first_last_name(self):
        m = bridge.normalize_message(
            {"update_id": 1, "message": {"text": "x", "chat": {"id": 1},
             "from": {"id": 2, "first_name": "Da", "last_name": "Ni"}}}, self.cfg)
        self.assertEqual(m["sender"], "Da Ni")

    def test_non_text_message_is_none(self):
        self.assertIsNone(bridge.normalize_message(
            {"update_id": 1, "message": {"chat": {"id": 1},
             "photo": []}}, self.cfg))

    def test_empty_after_sanitize_is_none(self):
        self.assertIsNone(bridge.normalize_message(self._update(text="\x00\x07"), self.cfg))

    def test_edit_or_channel_post_is_none(self):
        self.assertIsNone(bridge.normalize_message(
            {"update_id": 1, "edited_message": {"text": "x"}}, self.cfg))


class AllowlistTest(unittest.TestCase):
    def test_empty_allowlist_accepts_all(self):
        c = TelegramConfig.from_env({"HOME": "/tmp/x"})
        self.assertTrue(bridge.is_allowed(c, {"chat_id": 999}))

    def test_only_listed_chat_ids(self):
        c = TelegramConfig.from_env(
            {"HOME": "/tmp/x", "TELEGRAM_ALLOWED_CHAT_IDS": "42"})
        self.assertTrue(bridge.is_allowed(c, {"chat_id": 42}))
        self.assertFalse(bridge.is_allowed(c, {"chat_id": 43}))


class TokenTest(unittest.TestCase):
    def test_absent_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            lines, log = _log_sink()
            self.assertIsNone(bridge.read_token(cfg, log))
            self.assertTrue(any("gated" in x for x in lines))

    def test_reads_and_strips(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.token_file.write_text("  123:ABC\n")
            os.chmod(cfg.token_file, 0o600)
            lines, log = _log_sink()
            self.assertEqual(bridge.read_token(cfg, log), "123:ABC")

    def test_never_logs_token(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            secret = "999:SUPERSECRETVALUE"
            cfg.token_file.write_text(secret)
            os.chmod(cfg.token_file, 0o600)
            lines, log = _log_sink()
            bridge.read_token(cfg, log)
            self.assertFalse(any(secret in x for x in lines))

    def test_warns_on_loose_perms_but_returns_token(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.token_file.write_text("tok")
            os.chmod(cfg.token_file, 0o644)
            lines, log = _log_sink()
            self.assertEqual(bridge.read_token(cfg, log), "tok")
            self.assertTrue(any("WARNING" in x and "chmod 600" in x for x in lines))

    def test_empty_file_gated(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.token_file.write_text("   \n")
            os.chmod(cfg.token_file, 0o600)
            lines, log = _log_sink()
            self.assertIsNone(bridge.read_token(cfg, log))


class _FakeResp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ApiRequestTest(unittest.TestCase):
    def setUp(self):
        self.cfg = TelegramConfig.from_env({"HOME": "/tmp/x"})

    def test_ok_true_returns_result(self):
        body = json.dumps({"ok": True, "result": [{"update_id": 1}]})
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               return_value=_FakeResp(body)):
            lines, log = _log_sink()
            ok, res = bridge.api_request(self.cfg, "TOK", "getUpdates", {}, log)
        self.assertTrue(ok)
        self.assertEqual(res, [{"update_id": 1}])

    def test_ok_false_returns_false(self):
        body = json.dumps({"ok": False, "description": "Unauthorized"})
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               return_value=_FakeResp(body, 401)):
            lines, log = _log_sink()
            ok, res = bridge.api_request(self.cfg, "TOK", "getUpdates", {}, log)
        self.assertFalse(ok)

    def test_never_logs_token_in_url(self):
        # A transport error must not leak the token even though it's in the URL.
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               side_effect=OSError("connection refused")):
            lines, log = _log_sink()
            ok, res = bridge.api_request(self.cfg, "SECRET-TOKEN", "getUpdates", {}, log)
        self.assertFalse(ok)
        self.assertFalse(any("SECRET-TOKEN" in x for x in lines))

    def test_url_embeds_token_but_is_not_logged(self):
        self.assertIn("botSECRET",
                      bridge._method_url(self.cfg, "SECRET", "getUpdates"))

    def test_captures_request_shape(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["method"] = req.get_method()
            return _FakeResp(json.dumps({"ok": True, "result": "pong"}))

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            lines, log = _log_sink()
            ok, res = bridge.api_request(
                self.cfg, "TOK", "sendMessage", {"chat_id": 5, "text": "hi"}, log)
        self.assertTrue(ok)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"]), {"chat_id": 5, "text": "hi"})
        self.assertIn("/botTOK/sendMessage", captured["url"])


class SendMessageTest(unittest.TestCase):
    def test_sanitizes_outbound_text(self):
        cfg = TelegramConfig.from_env({"HOME": "/tmp/x"})
        sent = {}

        def fake_api(cfg, token, method, params, log, timeout=None):
            sent.update(params)
            return True, {}

        with mock.patch.object(bridge, "api_request", fake_api):
            lines, log = _log_sink()
            bridge.send_message(cfg, "TOK", 5, "he\x00llo", log)
        self.assertEqual(sent["text"], "hello")
        self.assertEqual(sent["chat_id"], 5)


class HandleUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = TelegramConfig.from_env({"HOME": "/tmp/x"})

    def _update(self, uid, text="hi", chat=42):
        return {"update_id": uid, "message": {"text": text, "chat": {"id": chat},
                "from": {"id": 9, "username": "boss"}, "date": 1}}

    def test_routes_and_advances_offset(self):
        posted = []
        state = {"offset": 0}
        n = bridge.handle_updates(
            state, [self._update(5), self._update(6)], self.cfg,
            (lambda m: None), poster=lambda cfg, msg, log: posted.append(msg) or True)
        self.assertEqual(n, 2)
        self.assertEqual(state["offset"], 7)  # last uid + 1
        self.assertEqual([p["text"] for p in posted], ["hi", "hi"])

    def test_offset_advances_even_when_dropped(self):
        cfg = TelegramConfig.from_env(
            {"HOME": "/tmp/x", "TELEGRAM_ALLOWED_CHAT_IDS": "1"})
        posted = []
        state = {"offset": 0}
        n = bridge.handle_updates(
            state, [self._update(9, chat=42)], cfg, (lambda m: None),
            poster=lambda *a: posted.append(a) or True)
        self.assertEqual(n, 0)          # dropped (chat 42 not allowed)
        self.assertEqual(state["offset"], 10)  # but offset still advanced
        self.assertEqual(posted, [])

    def test_non_text_update_advances_offset_only(self):
        state = {"offset": 0}
        n = bridge.handle_updates(
            state, [{"update_id": 3, "message": {"chat": {"id": 1}}}],
            self.cfg, (lambda m: None), poster=lambda *a: True)
        self.assertEqual(n, 0)
        self.assertEqual(state["offset"], 4)

    def test_failed_poster_does_not_raise(self):
        state = {"offset": 0}
        n = bridge.handle_updates(
            state, [self._update(1)], self.cfg, (lambda m: None),
            poster=lambda *a: False)
        self.assertEqual(n, 0)
        self.assertEqual(state["offset"], 2)


class FeedPostTest(unittest.TestCase):
    def setUp(self):
        self.cfg = TelegramConfig.from_env(
            {"HOME": "/tmp/x", "TELEGRAM_ANSWERS_SCRIPT": "/fake/answers.sh"})

    def test_invokes_answers_sh_via_argv(self):
        calls = {}

        class R:
            returncode = 0
            stderr = ""

        def fake_run(argv, **kw):
            calls["argv"] = argv
            calls["kw"] = kw
            return R()

        msg = {"sender": "boss", "text": "book me a slot", "chat_id": 1}
        ok = bridge.feed_post(self.cfg, msg, (lambda m: None), run=fake_run)
        self.assertTrue(ok)
        argv = calls["argv"]
        # argv (not a shell string) -> no shell-injection surface.
        self.assertEqual(argv[0], "bash")
        self.assertIn("/fake/answers.sh", argv)
        self.assertIn("post", argv)
        self.assertIn("book me a slot", argv)
        self.assertIn("--q", argv)
        # message text passed as its own argv element, never interpolated
        qi = argv.index("--q")
        self.assertEqual(argv[qi + 1], "book me a slot")

    def test_nonzero_return_is_failure(self):
        class R:
            returncode = 2
            stderr = "boom"

        ok = bridge.feed_post(self.cfg, {"sender": "x", "text": "y", "chat_id": 1},
                              (lambda m: None), run=lambda *a, **k: R())
        self.assertFalse(ok)

    def test_spawn_error_is_failure_not_raise(self):
        def boom(*a, **k):
            raise OSError("no bash")

        ok = bridge.feed_post(self.cfg, {"sender": "x", "text": "y", "chat_id": 1},
                              (lambda m: None), run=boom)
        self.assertFalse(ok)


class StateAndLockTest(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            lines, log = _log_sink()
            self.assertEqual(bridge.read_state(cfg, log), {"offset": 0})
            bridge.write_state(cfg, {"offset": 99})
            self.assertEqual(bridge.read_state(cfg, log)["offset"], 99)

    def test_corrupt_state_resets(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.state_file.write_text("{not json")
            lines, log = _log_sink()
            self.assertEqual(bridge.read_state(cfg, log), {"offset": 0})

    def test_lock_excludes_second_holder(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(Path(d))
            lines, log = _log_sink()
            self.assertTrue(bridge.acquire_lock(cfg, log))
            # Same live pid recorded -> a second acquire sees a running instance.
            self.assertFalse(bridge.acquire_lock(cfg, log))
            bridge.release_lock(cfg)
            self.assertTrue(bridge.acquire_lock(cfg, log))
            bridge.release_lock(cfg)


if __name__ == "__main__":
    unittest.main()
