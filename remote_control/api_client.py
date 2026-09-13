"""The code-sessions API client: keychain OAuth + urllib request helpers, the
paginated session list, archive/submit/fetch calls, and the session-state
parsing helpers shared across ``remote_control``.

This was originally the side-effect half of the usage-limit monitor
(``usage_limit/monitor.py`` + the shared bits of ``usage_limit/detect.py``).
The monitor was deprecated and removed in #175 (native Claude auto-resume
superseded it), but its API client is shared plumbing for the titles monitor,
``fork-all``, the session manager, relaunch/handoff, new-session, inventory and
more -- so it was extracted here, unchanged in behavior.

Auth: the OAuth token is read from the macOS keychain on demand (the always-
running ``claude`` servers keep it refreshed). Nothing here is logged that
could leak the token.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from .config import LOGDIR

# Matches the cloud refusal detail for both the org/monthly usage limit and the
# rolling 5h session limit.
LIMIT_RE = re.compile(r"(usage limit|session limit)", re.I)


@dataclass(frozen=True)
class ApiClientConfig:
    """Just what the code-sessions API client needs: where the keychain
    credential lives, the API base, and the HTTP timeout. ``home``/``logdir``
    are carried for callers that derive their own state/log paths from the same
    config object (e.g. the titles monitor's watcher lock/log)."""
    home: Path
    logdir: Path
    api_base: str
    keychain_service: str
    http_timeout: int

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ApiClientConfig":
        env = os.environ if env is None else env
        return cls(
            home=Path(env["HOME"]),
            logdir=Path(env.get("REMOTE_CONTROL_LOGDIR", LOGDIR)),
            api_base="https://api.anthropic.com/v1/code",
            keychain_service=env.get("REMOTE_CONTROL_KEYCHAIN_SERVICE",
                                     "Claude Code-credentials"),
            http_timeout=int(env.get("REMOTE_CONTROL_HTTP_TIMEOUT_SECS", "30")),
        )


# --------------------------------------------------------------------------- #
# Session-state parsing (pure)
# --------------------------------------------------------------------------- #
def post_turn_summary(session: dict) -> dict:
    """Present both at the top level (detail GET) and under external_metadata
    (list GET); prefer whichever is populated."""
    pts = session.get("post_turn_summary")
    if pts:
        return pts
    return (session.get("external_metadata") or {}).get("post_turn_summary") or {}


def limit_pause_detail(session: dict) -> Optional[str]:
    """The limit status_detail if this session is paused on a usage/session
    limit, else None."""
    if session.get("status") != "active":
        return None
    if session.get("worker_status") != "idle":
        return None  # running / requires_action / unspecified -> not stuck-on-limit
    pts = post_turn_summary(session)
    if pts.get("status_category") != "failed":
        return None
    detail = pts.get("status_detail") or ""
    if not LIMIT_RE.search(detail):
        return None  # some other failure (e.g. a path error)
    return detail


def resume_event_body(message: str) -> dict:
    """The wrapped user-turn event shape the /events endpoint accepts."""
    return {"events": [{
        "event_type": "user",
        "source": "client",
        "payload": {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": message}]},
        },
    }]}


# --------------------------------------------------------------------------- #
# Auth + API client
# --------------------------------------------------------------------------- #
def get_token(cfg: ApiClientConfig, log) -> Optional[str]:
    # Read the OAuth access token from the keychain fresh each cycle (never
    # logged). The running `claude` servers refresh it before expiry.
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", cfg.keychain_service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"keychain read failed: {e}")
        return None
    if out.returncode != 0:
        log(f"keychain read rc={out.returncode}")
        return None
    try:
        return json.loads(out.stdout)["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log("keychain payload missing claudeAiOauth.accessToken")
        return None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    }


def api_request(cfg: ApiClientConfig, method: str, path: str, token: str,
                body: Optional[dict] = None):
    # Returns (status_code|None, parsed_json_or_text). status None == transport
    # error (logged by caller).
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        cfg.api_base + path, data=data, headers=_headers(token), method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        code = e.code
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return None, str(e)
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw


PAGE_LIMIT = 100
MAX_PAGES = 100


def list_sessions(cfg: ApiClientConfig, token: str, log) -> Optional[List[dict]]:
    """Every session, following ``next_cursor`` until the API runs out.

    This used to issue a single ``?limit=100`` and return that page as if it
    were the whole world. On an account with ~936 sessions that silently hid
    ~90% of them, and every consumer treats "absent from this list" as a fact:
    the rehydrate sweep reads it as "session is gone", and the one-shot reaper
    would read it as "no live session -> safe to kill". A truncated page turns
    both into false positives, so paginate.

    Pages are de-duplicated by id: sessions can shift between pages while we
    walk them (the ordering is mutable), and without the id set a session that
    slid backwards would be yielded twice. A page contributing no new ids also
    ends the walk, which stops a server-side cursor that never advances from
    spinning us forever. Returns None (not a partial list) if any page fails,
    since a partial list is exactly the falsehood this function exists to kill.
    """
    out: List[dict] = []
    seen = set()
    cursor = None
    for page in range(MAX_PAGES):
        path = f"/sessions?limit={PAGE_LIMIT}"
        if cursor:
            path += f"&cursor={urllib.parse.quote(str(cursor), safe='')}"
        code, data = api_request(cfg, "GET", path, token)
        if code == 401:
            log("list sessions 401 (token stale?); skipping cycle")
            return None
        if code != 200 or not isinstance(data, dict):
            log(f"list sessions failed http={code} page={page} "
                f"body={str(data)[:200]}")
            return None
        batch = data.get("data") or []
        fresh = [s for s in batch
                 if isinstance(s, dict) and s.get("id") not in seen]
        for s in fresh:
            seen.add(s.get("id"))
        out.extend(fresh)
        cursor = data.get("next_cursor")
        if not cursor or not fresh:
            return out
    log(f"list sessions: stopped at MAX_PAGES={MAX_PAGES} ({len(out)} so far); "
        "returning partial page walk as a guard against a non-advancing cursor")
    return out


def archive_session(cfg: ApiClientConfig, token: str, sid: str, log) -> Tuple[int, object]:
    """Archive a session via ``POST /sessions/{id}/archive`` (verified live).

    Returns ``(http_code, body)``. Caller is responsible for treating 200 as
    success; non-200 is logged here for the common diagnostic path."""
    code, body = api_request(cfg, "POST", f"/sessions/{sid}/archive", token)
    if code != 200:
        log(f"archive {sid} failed http={code} body={str(body)[:200]}")
    return code, body


def submit_user_message(cfg: ApiClientConfig, token: str, sid: str,
                        message: str, log) -> Tuple[int, object]:
    """Submit a user-turn message into a session via ``POST /sessions/{id}/events``.

    Exposed as a stand-alone primitive so the ``sessions submit`` CLI (and any
    other caller) can drop a turn into an arbitrary live session.

    Returns ``(http_code, body)``; non-200 is logged for diagnostics."""
    code, body = api_request(
        cfg, "POST", f"/sessions/{sid}/events", token,
        resume_event_body(message))
    if code != 200:
        log(f"submit {sid} failed http={code} body={str(body)[:200]}")
    return code, body


def fetch_session_events(cfg: ApiClientConfig, token: str, sid: str,
                         log, limit: int = 500) -> Tuple[Optional[int], list]:
    """``GET /sessions/{id}/events`` -- the sibling of :func:`submit_user_message`.

    Returns ``(http_code, events)`` where *events* is the response's ``data``
    list (newest first, by ``sequence_num``). Used by ``relaunch`` to derive a
    brief when no local JSONL transcript exists (cloud-agent or cross-host
    bridge sessions, where the agent's events never get mirrored to
    ``~/.claude/projects``).

    *limit* caps how many events to ask for in one shot. The default 500
    covers most active sessions in a single request; pagination via
    ``next_cursor`` is the caller's job if it isn't enough.
    """
    code, body = api_request(
        cfg, "GET", f"/sessions/{sid}/events?limit={int(limit)}", token)
    if code != 200 or not isinstance(body, dict):
        log(f"fetch_session_events {sid} http={code} body={str(body)[:200]}")
        return code, []
    data = body.get("data")
    if not isinstance(data, list):
        return code, []
    return code, data


def fetch_session_state(cfg: ApiClientConfig, token: str, sid: str,
                        log) -> Tuple[Optional[int], str, str]:
    """``GET /sessions/{id}`` -> ``(http_code, status, connection_status)``.

    Used by the dispatcher inject path to tell a *submittable* session
    (``status == "active"`` AND ``connection_status == "connected"``) from a
    freshly pre-created one that the API will 409 on ("Session is not active").
    A just-pre-created ``--create-session-in-dir`` session sits in a transient
    non-active state until its server's TUI actually attaches it; submitting
    before then returns HTTP 409. ``code`` is None on transport error (caller
    treats that like "unknown, keep polling")."""
    code, data = api_request(cfg, "GET", f"/sessions/{sid}", token)
    if code != 200 or not isinstance(data, dict):
        if code is not None:
            log(f"session-state {sid} http={code} body={str(data)[:160]}")
        return code, "", ""
    d = data.get("response_shape", data)
    return code, (d.get("status") or ""), (d.get("connection_status") or "")
