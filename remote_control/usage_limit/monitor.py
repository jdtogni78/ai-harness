"""The usage-limit monitor's side effects: keychain auth, the code-sessions API
client, JSON state + pid-lock, the detect/resume loop, and ``main``.

Auth: the OAuth token is read from the macOS keychain each cycle (the always-
running ``claude`` servers keep it refreshed). DRY_RUN (default ON) detects and
logs would-be resumes without firing any POST.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from ..config import CodexConfig, UsageLimitConfig
from ..logging_util import make_logger
from .codex import backoff_until_reset, codex_blocked_sessions, load_index
from .detect import (
    LIMIT_RE,
    compute_backoff,
    limit_pause_detail,
    pick_resume_target,
    post_turn_summary,
    resume_event_body,
)

_running = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Auth + API client
# --------------------------------------------------------------------------- #
def get_token(cfg: UsageLimitConfig, log) -> Optional[str]:
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


def api_request(cfg: UsageLimitConfig, method: str, path: str, token: str,
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


def list_sessions(cfg: UsageLimitConfig, token: str, log) -> Optional[List[dict]]:
    code, data = api_request(cfg, "GET", "/sessions?limit=100", token)
    if code == 401:
        log("list sessions 401 (token stale?); skipping cycle")
        return None
    if code != 200 or not isinstance(data, dict):
        log(f"list sessions failed http={code} body={str(data)[:200]}")
        return None
    return data.get("data", [])


def archive_session(cfg: UsageLimitConfig, token: str, sid: str, log) -> Tuple[int, object]:
    """Archive a session via ``POST /sessions/{id}/archive`` (verified live).

    Returns ``(http_code, body)``. Caller is responsible for treating 200 as
    success; non-200 is logged here for the common diagnostic path."""
    code, body = api_request(cfg, "POST", f"/sessions/{sid}/archive", token)
    if code != 200:
        log(f"archive {sid} failed http={code} body={str(body)[:200]}")
    return code, body


def submit_user_message(cfg: UsageLimitConfig, token: str, sid: str,
                        message: str, log) -> Tuple[int, object]:
    """Submit a user-turn message into a session via ``POST /sessions/{id}/events``.

    Same path the usage-limit monitor uses to deliver its resume nudge -- exposed
    here as a stand-alone primitive so the ``sessions submit`` CLI (and any other
    caller) can drop a turn into an arbitrary live session.

    Returns ``(http_code, body)``; non-200 is logged for diagnostics."""
    code, body = api_request(
        cfg, "POST", f"/sessions/{sid}/events", token,
        resume_event_body(message))
    if code != 200:
        log(f"submit {sid} failed http={code} body={str(body)[:200]}")
    return code, body


def fetch_session_events(cfg: UsageLimitConfig, token: str, sid: str,
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


def still_limit_paused(cfg: UsageLimitConfig, sid: str, token: str) -> Tuple[Optional[bool], str]:
    code, data = api_request(cfg, "GET", f"/sessions/{sid}", token)
    if code != 200 or not isinstance(data, dict):
        return None, ""
    d = data.get("response_shape", data)
    pts = post_turn_summary(d)
    detail = pts.get("status_detail") or ""
    paused = pts.get("status_category") == "failed" and bool(LIMIT_RE.search(detail))
    return paused, detail


def fetch_session_state(cfg: UsageLimitConfig, token: str, sid: str,
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


# --------------------------------------------------------------------------- #
# Detect + resume
# --------------------------------------------------------------------------- #
def detect(state: dict, token: str, cfg: UsageLimitConfig, log,
           now: Optional[float] = None) -> None:
    sessions = list_sessions(cfg, token, log)
    if sessions is None:
        return
    now = time.time() if now is None else now
    discovered = set()
    for s in sessions:
        sid = s.get("id")
        if not sid or sid in cfg.skip_session_ids:
            continue
        detail = limit_pause_detail(s)
        if detail is None:
            continue
        discovered.add(sid)
        ent = state["sessions"].get(sid)
        if ent is None:
            state["sessions"][sid] = {
                "cse_id": sid,
                "env_kind": s.get("environment_kind"),
                "title": s.get("title"),
                "status_detail": detail,
                "first_seen": now_iso(),
                "attempts": 0,
                "last_attempt_at": None,
                "next_attempt_at": now,
            }
            log(f"detected paused {sid} env={s.get('environment_kind')} "
                f"title={s.get('title')!r} detail={detail!r}")
        else:
            ent["status_detail"] = detail  # keep backoff fields
            ent["title"] = s.get("title")

    # Sessions we tracked that the API no longer reports as limit-paused have
    # recovered (resumed, or the worker moved on) -> stop tracking them.
    for sid in list(state["sessions"].keys()):
        if sid not in discovered:
            log(f"{sid} no longer limit-paused; clearing")
            del state["sessions"][sid]

    # GC: anything paused for more than gc_age_secs is effectively abandoned.
    cutoff = now - cfg.gc_age_secs
    for sid in list(state["sessions"].keys()):
        ts = state["sessions"][sid].get("first_seen") or ""
        try:
            first = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            first = now
        if first < cutoff:
            log(f"gc {sid} (first_seen {ts})")
            del state["sessions"][sid]


def _apply_backoff(s: dict, detail: Optional[str], cfg: UsageLimitConfig, log) -> None:
    next_at, msg = compute_backoff(
        detail or s.get("status_detail", ""), s.get("attempts", 1),
        time.time(), cfg.backoffs_secs)
    s["next_attempt_at"] = next_at
    log(msg)


def attempt_resume(s: dict, token: str, cfg: UsageLimitConfig, log,
                   sleep=time.sleep) -> None:
    sid = s["cse_id"]
    if cfg.dry_run:
        log(f"[dry-run] would resume {sid} env={s.get('env_kind')} "
            f"title={s.get('title')!r} detail={s.get('status_detail')!r}")
        # Don't hammer the log every resume tick: defer the next dry-run notice.
        s["next_attempt_at"] = time.time() + cfg.resume_interval
        return

    s["attempts"] = s.get("attempts", 0) + 1
    s["last_attempt_at"] = now_iso()
    log(f"resume start {sid} attempt={s['attempts']} env={s.get('env_kind')}")

    code, body = api_request(
        cfg, "POST", f"/sessions/{sid}/events", token,
        resume_event_body(cfg.resume_message))
    if code != 200:
        log(f"resume POST failed {sid} http={code} body={str(body)[:200]}")
        _apply_backoff(s, None, cfg, log)
        return

    sleep(cfg.resume_verify_secs)
    paused, detail = still_limit_paused(cfg, sid, token)
    if paused is False:
        log(f"resume success {sid}")
        # Leave it tracked; next detect tick clears it. Push next_attempt_at out
        # so we don't re-fire meanwhile.
        s["next_attempt_at"] = time.time() + cfg.resume_interval
    else:
        log(f"resume still limited {sid} detail={detail!r}")
        _apply_backoff(s, detail, cfg, log)


# --------------------------------------------------------------------------- #
# Codex (local CLI) detect + resume
# --------------------------------------------------------------------------- #
def detect_codex(state: dict, cfg: CodexConfig, log,
                 now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    titles = load_index(str(cfg.index_file))
    blocked = codex_blocked_sessions(
        str(cfg.sessions_dir), now, cfg.block_threshold, cfg.recent_secs, titles)

    def _refresh(ent: dict, b: dict) -> None:
        ent["status_detail"] = b.get("status_detail")
        ent["reset_epoch"] = b.get("reset_epoch")
        ent["cwd"] = b.get("cwd")
        ent["title"] = b.get("title")

    discovered = set()
    for b in blocked:
        sid = b["codex_id"]
        if sid in cfg.skip_session_ids:
            continue
        discovered.add(sid)
        ent = state["sessions"].get(sid)
        if ent is None:
            state["sessions"][sid] = {
                "codex_id": sid,
                "cwd": b.get("cwd"),
                "title": b.get("title"),
                "status_detail": b.get("status_detail"),
                "reset_epoch": b.get("reset_epoch"),
                "first_seen": now_iso(),
                "attempts": 0,
                "last_attempt_at": None,
                "next_attempt_at": now,
                "pid": None,
                "spawned_at": None,
            }
            log(f"detected codex paused {sid} cwd={b.get('cwd')!r} "
                f"title={b.get('title')!r} detail={b.get('status_detail')!r}")
        else:
            _refresh(ent, b)

    # A paused session stops appending to its rollout, so it drops out of the
    # conservative recent_secs scan above long before a weekly/monthly window
    # resets -- yet it is still rate-limited and *will* be resumable once that
    # window refills. To keep that resume alive ("maintain the resume when
    # possible") without resurrecting genuinely-abandoned sessions, re-check only
    # the already-tracked-but-undiscovered ones against the full rollout history
    # (bounded by gc_age_secs, which also bounds how long we hold them): still
    # blocked -> keep and refresh its reset target; recovered (newest rollout
    # healthy) or gone -> clear.
    pending = [sid for sid in state["sessions"] if sid not in discovered]
    still_blocked: dict = {}
    if pending:
        still_blocked = {b["codex_id"]: b for b in codex_blocked_sessions(
            str(cfg.sessions_dir), now, cfg.block_threshold, cfg.gc_age_secs, titles)}
    for sid in pending:
        ent = state["sessions"][sid]
        if _codex_pid_alive(ent, now, cfg):
            continue  # in-flight resume; don't double-fire
        b = still_blocked.get(sid)
        if b is not None:
            _refresh(ent, b)  # quiet but still rate-limited -> keep, refresh reset
            continue
        log(f"codex {sid} no longer limit-blocked; clearing")
        del state["sessions"][sid]

    # GC: drop entries abandoned for more than gc_age_secs -- but never evict one
    # still waiting on a future reset, or we'd throw away a resume we can make.
    cutoff = now - cfg.gc_age_secs
    for sid in list(state["sessions"].keys()):
        ent = state["sessions"][sid]
        reset = ent.get("reset_epoch")
        if reset and reset > now:
            continue
        ts = ent.get("first_seen") or ""
        try:
            first = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            first = now
        if first < cutoff:
            log(f"gc codex {sid} (first_seen {ts})")
            del state["sessions"][sid]


def _codex_pid_alive(ent: dict, now: float, cfg: CodexConfig) -> bool:
    """A spawned `codex exec resume` is considered still running (don't re-fire)
    until it exits or max_runtime elapses."""
    pid = ent.get("pid")
    if not pid:
        return False
    spawned = ent.get("spawned_at")
    if spawned and now - spawned > cfg.max_runtime:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def pick_codex_target(state: dict, now: float, cfg: CodexConfig) -> Optional[dict]:
    avail = {sid: e for sid, e in state["sessions"].items()
             if not _codex_pid_alive(e, now, cfg)}
    return pick_resume_target(avail, now, cfg.max_attempts)


def attempt_resume_codex(s: dict, cfg: CodexConfig, log,
                         now: Optional[float] = None, popen=None) -> None:
    """Resume a Codex session by spawning a detached ``codex exec resume`` in its
    recorded cwd. Detached (not awaited) because the resumed turn runs the whole
    task synchronously -- we must not block the monitor loop on it."""
    popen = subprocess.Popen if popen is None else popen
    now = time.time() if now is None else now
    sid = s["codex_id"]
    cwd = s.get("cwd")
    if cfg.dry_run:
        log(f"[dry-run] would resume codex {sid} cwd={cwd!r} "
            f"title={s.get('title')!r} detail={s.get('status_detail')!r}")
        s["next_attempt_at"] = now + cfg.resume_interval
        return
    if not cwd or not os.path.isdir(cwd):
        log(f"codex resume skip {sid}: cwd missing {cwd!r}")
        s["next_attempt_at"] = now + cfg.resume_interval
        return
    if not cfg.codex_bin.exists():
        log(f"codex resume skip {sid}: codex bin not found {cfg.codex_bin}")
        s["next_attempt_at"] = now + cfg.resume_interval
        return

    s["attempts"] = s.get("attempts", 0) + 1
    s["last_attempt_at"] = now_iso()
    cfg.resume_logdir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.resume_logdir / f"{sid}.log"
    cmd = [str(cfg.codex_bin), "exec", "resume", sid, cfg.resume_message]
    try:
        out = open(out_path, "ab")
    except OSError as e:
        log(f"codex resume log open failed {sid}: {e}")
        out = subprocess.DEVNULL
    try:
        proc = popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                     stdout=out, stderr=out, start_new_session=True)
    except (OSError, ValueError) as e:
        log(f"codex resume spawn failed {sid}: {e}")
        next_at, msg = backoff_until_reset(
            s.get("reset_epoch"), s["attempts"], now, cfg.backoffs_secs)
        s["next_attempt_at"] = next_at
        log(msg)
        return
    finally:
        # The child inherited its own fd; the parent's copy is no longer needed.
        if hasattr(out, "close"):
            out.close()
    s["pid"] = proc.pid
    s["spawned_at"] = now
    log(f"codex resume spawned {sid} pid={proc.pid} attempt={s['attempts']} "
        f"cwd={cwd} -> {out_path}")
    # Hold off until the run could plausibly finish AND any window has reset.
    next_at, msg = backoff_until_reset(
        s.get("reset_epoch"), s["attempts"], now, cfg.backoffs_secs)
    s["next_attempt_at"] = max(next_at, now + cfg.resume_interval)
    log(msg)


# --------------------------------------------------------------------------- #
# State + lock
# --------------------------------------------------------------------------- #
def read_state(cfg: UsageLimitConfig, log) -> dict:
    if not cfg.state_file.exists():
        return {"sessions": {}}
    try:
        data = json.loads(cfg.state_file.read_text())
        data.setdefault("sessions", {})
        return data
    except json.JSONDecodeError:
        log("state file corrupt; starting fresh")
        return {"sessions": {}}


def write_state(cfg: UsageLimitConfig, state: dict) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.state_file)


def acquire_lock(cfg: UsageLimitConfig, log) -> bool:
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


def release_lock(cfg: UsageLimitConfig) -> None:
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
    cfg = UsageLimitConfig.from_env()
    codex_cfg = CodexConfig.from_env()
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
        log(f"monitor up pid={os.getpid()} v2-api detect={cfg.detect_interval}s "
            f"resume={cfg.resume_interval}s dry_run={cfg.dry_run} "
            f"codex={'on' if codex_cfg.enabled else 'off'}")
        if not cfg.state_file.exists():
            write_state(cfg, {"sessions": {}})
        if codex_cfg.enabled and not codex_cfg.state_file.exists():
            write_state(codex_cfg, {"sessions": {}})

        # Title-prefix re-apply moved out to its own daemon -- see
        # ``python3 -m remote_control titles watch`` and the
        # com.<user>.claude-titles-monitor LaunchAgent. This loop now only
        # handles the usage-limit detect/resume cycle. The new daemon reads
        # SESSION_TITLE_APPLY_SECS directly; nothing here owns that knob.
        last_detect = 0.0
        last_resume = 0.0
        while _running:
            now = time.time()
            if now - last_detect >= cfg.detect_interval:
                last_detect = now
                token = get_token(cfg, log)
                if token:
                    state = read_state(cfg, log)
                    detect(state, token, cfg, log)
                    write_state(cfg, state)
                if codex_cfg.enabled:
                    cstate = read_state(codex_cfg, log)
                    detect_codex(cstate, codex_cfg, log)
                    write_state(codex_cfg, cstate)
            if now - last_resume >= cfg.resume_interval:
                last_resume = now
                state = read_state(cfg, log)
                target = pick_resume_target(state["sessions"], time.time(), cfg.max_attempts)
                if target is not None:
                    token = get_token(cfg, log)
                    if token:
                        attempt_resume(target, token, cfg, log)
                        write_state(cfg, state)
                if codex_cfg.enabled:
                    cstate = read_state(codex_cfg, log)
                    ctarget = pick_codex_target(cstate, time.time(), codex_cfg)
                    if ctarget is not None:
                        attempt_resume_codex(ctarget, codex_cfg, log)
                        write_state(codex_cfg, cstate)
            # 1s tick bounds SIGTERM latency (time.sleep returns after the
            # handler runs, not when the signal arrives).
            time.sleep(1)
        log("monitor exiting")
        return 0
    finally:
        release_lock(cfg)
