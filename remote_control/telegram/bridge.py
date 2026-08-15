"""The Telegram inbound bridge's side effects: token read, the Bot API client,
inbound sanitization + routing into the durable answers feed, and the supervised
long-poll loop.

Design mirrors ``remote_control/usage_limit/monitor.py`` on purpose (#163): a
supervised long-poll loop, JSON state + a pid-lock, SIGTERM-clean shutdown, and
env->config via :class:`~remote_control.config.TelegramConfig`. No webhook / no
public URL -- the home host is behind NAT, so we poll (same reason the usage
monitor polls the code-sessions API).

Security posture (settled in #163):
  * The bot token is read FRESH from a chmod-600 file each time it's needed and
    is NEVER logged, and NEVER placed anywhere a log line could capture it (the
    token sits in the Telegram URL path, so we never log the URL either).
  * All inbound message content is UNTRUSTED. :func:`sanitize_text` strips
    control characters / null bytes and caps length before the text reaches the
    feed, a render, or any subprocess. The feed poster is invoked via argv (no
    shell), so there is no shell-injection surface even before sanitization.
  * The test suite makes NO live authenticated calls (cf #114): ``api_request``
    and the feed poster are injectable, and ``main`` gates the live poll behind
    the token file's existence.

The reply/feed seam #164 (propose-and-confirm scheduler) will consume:
  * :func:`normalize_message` -- the stable inbound shape written to the feed;
  * :func:`send_message` -- outbound ``sendMessage`` to reply/confirm.
#164 is strictly propose-and-confirm (no auto-book); this bridge only plumbs
messages in and replies out, it makes no booking decisions.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from ..config import TelegramConfig
from ..logging_util import make_logger

_running = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def read_token(cfg: TelegramConfig, log) -> Optional[str]:
    """Read the bot token from the chmod-600 file, fresh each call.

    Returns None (and logs a token-FREE diagnostic) when the file is missing or
    empty -- that is the "not yet provisioned, live poll gated" state, not an
    error. The token itself is never logged. Warns (but still returns the token)
    when the file's mode is group/other-readable, so a mis-permissioned drop is
    visible without blocking the boss.
    """
    path = cfg.token_file
    try:
        st = path.stat()
    except OSError:
        log(f"token file absent ({path}); live poll gated until the boss drops it")
        return None
    mode = st.st_mode & 0o077
    if mode:
        log(f"WARNING token file {path} is group/other-accessible "
            f"(mode {oct(st.st_mode & 0o777)}); expected chmod 600")
    try:
        raw = path.read_text()
    except OSError as e:
        log(f"token file read failed ({path}): {e}")
        return None
    token = raw.strip()
    if not token:
        log(f"token file empty ({path}); live poll gated")
        return None
    return token


# --------------------------------------------------------------------------- #
# Bot API client
# --------------------------------------------------------------------------- #
def _method_url(cfg: TelegramConfig, token: str, method: str) -> str:
    # The token lives in the URL path -> this string is a secret. Never log it.
    return f"{cfg.api_base}/bot{token}/{method}"


def api_request(cfg: TelegramConfig, token: str, method: str, params: dict, log,
                timeout: Optional[int] = None) -> Tuple[bool, object]:
    """POST ``params`` (JSON) to Bot API ``method``.

    Returns ``(ok, result_or_error)``:
      * ``(True, result)`` when Telegram returns ``{"ok": true, "result": ...}``;
      * ``(False, detail)`` on transport error, non-200, or ``ok: false``.
    Only the METHOD NAME is ever logged, never the URL (which embeds the token).
    """
    url = _method_url(cfg, token, method)
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"content-type": "application/json"}, method="POST")
    to = cfg.http_timeout if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=to) as r:
            raw = r.read().decode("utf-8", errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        code = e.code
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        # str(e) can't contain the token (it's not in params, only in the URL,
        # which urllib's error messages for these branches don't echo back).
        log(f"telegram {method} transport error: {e}")
        return False, str(e)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        log(f"telegram {method} http={code} non-JSON body")
        return False, raw
    if code != 200 or not isinstance(body, dict) or not body.get("ok"):
        desc = body.get("description") if isinstance(body, dict) else None
        log(f"telegram {method} http={code} ok=False desc={desc!r}")
        return False, desc or body
    return True, body.get("result")


def get_updates(cfg: TelegramConfig, token: str, offset: int, log) -> Optional[list]:
    """Long-poll ``getUpdates`` from *offset*. Returns the updates list (possibly
    empty) or None on failure. Socket timeout is poll_timeout + http_timeout so
    the held-open long poll doesn't trip the client timeout."""
    ok, result = api_request(
        cfg, token, "getUpdates",
        {"offset": offset, "timeout": cfg.poll_timeout},
        log, timeout=cfg.poll_timeout + cfg.http_timeout)
    if not ok:
        return None
    if not isinstance(result, list):
        log(f"getUpdates: unexpected result type {type(result).__name__}")
        return None
    return result


def send_message(cfg: TelegramConfig, token: str, chat_id, text: str,
                 log) -> Tuple[bool, object]:
    """Outbound ``sendMessage`` -- the reply primitive Phase-B/#164 needs.

    Text is sanitized here too (defense in depth: a caller might forward
    partially-attacker-controlled content). Returns ``(ok, result_or_error)``.
    """
    safe = sanitize_text(text, cfg.max_message_len)
    return api_request(cfg, token, "sendMessage",
                       {"chat_id": chat_id, "text": safe}, log)


# --------------------------------------------------------------------------- #
# Sanitization + normalization (the seam #164 reads)
# --------------------------------------------------------------------------- #
def sanitize_text(text: object, max_len: int) -> str:
    """Make untrusted inbound text safe to feed/render/pass to a subprocess.

    Drops NULs and C0/C1 control characters (keeping ``\\t`` and ``\\n``),
    collapses to a stripped string, and hard-caps length (appending an ellipsis
    marker so truncation is visible). Non-str input -> coerced via ``str``.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    out = []
    for ch in text:
        o = ord(ch)
        if ch in ("\t", "\n"):
            out.append(ch)
        elif o < 0x20 or 0x7F <= o <= 0x9F:
            continue  # strip control chars (incl. NUL) that could corrupt render
        else:
            out.append(ch)
    cleaned = "".join(out).strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[: max(0, max_len - 1)].rstrip() + "…"
    return cleaned


def normalize_message(update: dict, cfg: TelegramConfig) -> Optional[dict]:
    """Turn a raw Telegram update into the stable inbound record we route + that
    #164 consumes. Returns None for updates that aren't a text message we handle
    (edits, channel posts, non-text, etc.) so the loop can still advance offset.

    Shape::

        {"update_id": int, "chat_id": <id>, "sender": str,
         "sender_id": <id>, "text": str, "date": int}

    ``text`` is already sanitized. ``sender`` is a display label only (also
    sanitized) -- never trust it for auth; use ``chat_id`` against
    ``allowed_chat_ids`` for that.
    """
    if not isinstance(update, dict):
        return None
    uid = update.get("update_id")
    msg = update.get("message")
    if not isinstance(msg, dict) or "text" not in msg:
        return None
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    text = sanitize_text(msg.get("text"), cfg.max_message_len)
    if not text:
        return None
    name = frm.get("username") or " ".join(
        p for p in (frm.get("first_name"), frm.get("last_name")) if p) or "unknown"
    return {
        "update_id": uid,
        "chat_id": chat.get("id"),
        "sender": sanitize_text(name, 80),
        "sender_id": frm.get("id"),
        "text": text,
        "date": msg.get("date"),
    }


def is_allowed(cfg: TelegramConfig, msg: dict) -> bool:
    """True if the message's chat is permitted. Empty allowlist => accept all
    (spike default). The check is on ``chat_id`` (Telegram-assigned), never on
    the spoofable display ``sender``."""
    if not cfg.allowed_chat_ids:
        return True
    return str(msg.get("chat_id")) in cfg.allowed_chat_ids


# --------------------------------------------------------------------------- #
# Routing into the durable answers feed (+ phone push)
# --------------------------------------------------------------------------- #
def feed_post(cfg: TelegramConfig, msg: dict, log,
              run=subprocess.run) -> bool:
    """Land one inbound message into the durable answers feed via ``answers.sh
    post`` (argv, no shell). answers.sh handles all three durable effects:
    prepend to the iCloud file, mirror into the pinned inbox session, and fire
    the phone PushNotification (via the inbox brief). We map an inbound boss
    message to a feed entry with the message as the **Q** and a pending **A**
    (an inbound question awaiting a reply -- which #164 will fill/confirm).

    Returns True on success. Never raises on a feed failure -- a dropped feed
    post must not wedge the poll loop or lose the offset advance; it's logged
    and the loop moves on (the update_id is already consumed).
    """
    sender = msg.get("sender") or "unknown"
    subject = f"Telegram · {sender}"
    argv = [
        "bash", str(cfg.answers_script), "post",
        "--mgr", cfg.feed_tag,
        "--subject", subject,
        "--q", msg["text"],
        "--a", "(inbound — awaiting reply)",
    ]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"feed post failed to spawn: {e}")
        return False
    if proc.returncode != 0:
        # stderr may echo the message text (untrusted) but never the token.
        log(f"feed post rc={proc.returncode}: {(proc.stderr or '').strip()[:300]}")
        return False
    return True


# --------------------------------------------------------------------------- #
# Handle a batch of updates
# --------------------------------------------------------------------------- #
def handle_updates(state: dict, updates: list, cfg: TelegramConfig,
                   log, poster: Callable = feed_post) -> int:
    """Route each update, advancing the persisted offset past every update_id we
    consume (even ones we drop/reject) so we never re-fetch them. Returns the
    number of messages actually routed to the feed."""
    routed = 0
    for update in updates:
        uid = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(uid, int):
            # +1 => Telegram treats prior updates as confirmed on next poll.
            state["offset"] = max(state.get("offset", 0), uid + 1)
        msg = normalize_message(update, cfg)
        if msg is None:
            continue
        if not is_allowed(cfg, msg):
            log(f"drop message from chat_id={msg.get('chat_id')} "
                "(not in TELEGRAM_ALLOWED_CHAT_IDS)")
            continue
        if poster(cfg, msg, log):
            routed += 1
            log(f"routed update_id={msg['update_id']} chat_id={msg['chat_id']} "
                f"sender={msg['sender']!r} len={len(msg['text'])}")
    return routed


# --------------------------------------------------------------------------- #
# State + lock  (same shape as the usage monitor)
# --------------------------------------------------------------------------- #
def read_state(cfg: TelegramConfig, log) -> dict:
    if not cfg.state_file.exists():
        return {"offset": 0}
    try:
        data = json.loads(cfg.state_file.read_text())
        if not isinstance(data, dict):
            raise json.JSONDecodeError("not an object", "", 0)
        data.setdefault("offset", 0)
        return data
    except (json.JSONDecodeError, OSError):
        log("state file corrupt/unreadable; starting from offset 0")
        return {"offset": 0}


def write_state(cfg: TelegramConfig, state: dict) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.state_file)


def acquire_lock(cfg: TelegramConfig, log) -> bool:
    cfg.lock_file.parent.mkdir(parents=True, exist_ok=True)
    if cfg.lock_file.exists():
        try:
            pid = int(cfg.lock_file.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        else:
            log(f"another instance running pid={pid}; exiting")
            return False
    cfg.lock_file.write_text(str(os.getpid()))
    return True


def release_lock(cfg: TelegramConfig) -> None:
    try:
        cfg.lock_file.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    global _running
    _running = True
    cfg = TelegramConfig.from_env()
    cfg.logdir.mkdir(parents=True, exist_ok=True)
    log = make_logger(cfg.log_file, utc=True)

    def handler(signum, _frame):
        global _running
        log(f"received signal {signum}; shutting down")
        _running = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    if not acquire_lock(cfg, log):
        return 0
    try:
        log(f"telegram-bridge up pid={os.getpid()} poll_timeout={cfg.poll_timeout}s "
            f"dry_run={cfg.dry_run} allowlist={len(cfg.allowed_chat_ids)} "
            f"token_file={cfg.token_file}")
        if not cfg.state_file.exists():
            write_state(cfg, {"offset": 0})

        # Backoff when the token is missing (gated) or the API errors, so we
        # don't hot-loop. Reset to 0 on any successful poll.
        idle_backoff = 5
        while _running:
            token = read_token(cfg, log)
            if token is None:
                # Live poll gated: sleep in 1s ticks (bounded SIGTERM latency)
                # until the boss drops the token file, then resume.
                _interruptible_sleep(min(idle_backoff, 60))
                idle_backoff = min(idle_backoff * 2, 60)
                continue

            state = read_state(cfg, log)
            updates = get_updates(cfg, token, state.get("offset", 0), log)
            token = None  # drop the secret from the frame ASAP
            if updates is None:
                _interruptible_sleep(min(idle_backoff, 60))
                idle_backoff = min(idle_backoff * 2, 60)
                continue
            idle_backoff = 5
            if updates:
                handle_updates(state, updates, cfg, log)
                write_state(cfg, state)
            # Long poll already blocked up to poll_timeout; a tiny yield keeps
            # the loop responsive to SIGTERM between polls.
            _interruptible_sleep(1)
        log("telegram-bridge exiting")
        return 0
    finally:
        release_lock(cfg)


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in 1s ticks so a SIGTERM (which flips ``_running``) is honored
    within ~1s instead of after the full interval."""
    for _ in range(max(1, int(seconds))):
        if not _running:
            return
        time.sleep(1)
