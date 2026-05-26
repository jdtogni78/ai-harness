"""Pure detection / parsing / backoff for the Codex usage-limit monitor.

Codex (the local CLI / Desktop app) is the local-process sibling of the cloud
Claude monitor in ``detect.py``. There is no cloud session to POST a resume
into: each session is a ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``
log whose ``token_count`` events carry a ``rate_limits`` block, e.g.::

    {"limit_id":"codex","plan_type":"plus",
     "primary":   {"used_percent":90,"window_minutes":300,  "resets_at":1779433580},
     "secondary": {"used_percent":48,"window_minutes":10080,"resets_at":1779871017},
     "rate_limit_reached_type": null}

``primary`` is the rolling 5h window; ``secondary`` the weekly window. We treat a
session as blocked when ``rate_limit_reached_type`` is set or a window's
``used_percent`` reaches the threshold, and we wait until that window's
``resets_at`` before resuming (re-prompting an exhausted window is futile).

Functions here are pure-ish: parsing and the block/backoff math take no clock of
their own (callers pass ``now``); only the directory scanner touches the fs, and
it takes explicit paths so tests drive it with temp rollouts.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

WINDOWS = ("primary", "secondary")
_WINDOW_LABEL = {"primary": "5h", "secondary": "weekly"}


def read_rollout(path: str) -> Optional[dict]:
    """Extract ``{id, cwd, rate_limits}`` from a rollout JSONL, using the last
    ``token_count`` event that carried a ``rate_limits`` block. Returns None if
    the file has no ``session_meta`` (not a usable rollout)."""
    sid = cwd = None
    rate_limits = None
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "session_meta":
                    payload = obj.get("payload", {})
                    sid = payload.get("id")
                    cwd = payload.get("cwd")
                elif kind == "event_msg":
                    payload = obj.get("payload", {})
                    if payload.get("type") == "token_count" and payload.get("rate_limits"):
                        rate_limits = payload["rate_limits"]
    except OSError:
        return None
    if not sid:
        return None
    return {"id": sid, "cwd": cwd, "rate_limits": rate_limits}


def evaluate_block(
    rate_limits: Optional[dict],
    threshold: float,
) -> Tuple[bool, Optional[float], str]:
    """``(blocked, reset_epoch, detail)`` for a rate_limits block.

    Blocked when ``rate_limit_reached_type`` is set or any window's
    ``used_percent`` >= threshold. ``reset_epoch`` is the *latest* resets_at among
    the triggering windows -- you are only truly unblocked once the binding
    window refills (so two exhausted windows wait for the later one)."""
    if not rate_limits:
        return False, None, ""
    reached = rate_limits.get("rate_limit_reached_type")
    triggering = []
    for name in WINDOWS:
        win = rate_limits.get(name)
        if not isinstance(win, dict):
            continue
        pct = win.get("used_percent")
        if pct is not None and pct >= threshold:
            triggering.append((name, win))
    # rate_limit_reached_type, when it names a window, is authoritative: wait on
    # *that* window, not the latest of all of them.
    if reached in WINDOWS and isinstance(rate_limits.get(reached), dict):
        if all(n != reached for n, _ in triggering):
            triggering.append((reached, rate_limits[reached]))
    blocked = bool(reached) or bool(triggering)
    if not blocked:
        return False, None, ""
    # reached set but unmapped (no window over threshold, name unknown) -> wait on
    # the shortest known window rather than over-waiting on the weekly one.
    windows = triggering
    if not windows:
        for name in WINDOWS:
            if isinstance(rate_limits.get(name), dict):
                windows = [(name, rate_limits[name])]
                break
    resets = [w.get("resets_at") for _, w in windows if w.get("resets_at")]
    reset_epoch = max(resets) if resets else None
    parts = []
    for name, win in windows:
        pct = win.get("used_percent")
        parts.append(f"{_WINDOW_LABEL.get(name, name)} {pct:.0f}%")
    detail = "codex usage limit (" + ", ".join(parts) + ")"
    if reached:
        detail += f" reached={reached}"
    return True, reset_epoch, detail


def backoff_until_reset(
    reset_epoch: Optional[float],
    attempts: int,
    now: float,
    backoffs_secs: Sequence[int],
) -> Tuple[float, str]:
    """Next-attempt epoch + a log line. Wait until just after the window resets
    if we know when that is; otherwise exponential-ish backoff capped at the last
    step (mirrors ``detect.compute_backoff`` but uses an explicit epoch)."""
    if reset_epoch and reset_epoch > now:
        when = datetime.fromtimestamp(reset_epoch, timezone.utc)
        return reset_epoch + 30, f"  next attempt at reset {when:%Y-%m-%d %H:%M}Z"
    idx = min(max(attempts - 1, 0), len(backoffs_secs) - 1)
    delay = backoffs_secs[idx]
    return now + delay, f"  next attempt in {delay}s"


def load_index(index_file: str) -> Dict[str, str]:
    """``{session_id: thread_name}`` from ``session_index.jsonl`` (best effort)."""
    titles: Dict[str, str] = {}
    try:
        with open(index_file, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id"):
                    titles[obj["id"]] = obj.get("thread_name")
    except OSError:
        pass
    return titles


def _iter_rollout_files(sessions_dir: str) -> List[str]:
    found: List[str] = []
    for root, _dirs, files in os.walk(sessions_dir):
        for name in files:
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                found.append(os.path.join(root, name))
    return found


def codex_blocked_sessions(
    sessions_dir: str,
    now: float,
    threshold: float,
    recent_secs: int,
    titles: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """Scan rollouts modified within ``recent_secs`` and return one dict per
    blocked session (deduped to its newest rollout). Each dict::

        {codex_id, cwd, title, status_detail, reset_epoch, rollout, mtime}
    """
    titles = titles or {}
    # Newest rollout per session id wins (a resume forks a fresh rollout).
    newest: Dict[str, Tuple[float, str]] = {}
    for path in _iter_rollout_files(sessions_dir):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime > recent_secs:
            continue
        meta = read_rollout(path)
        if not meta:
            continue
        sid = meta["id"]
        if sid not in newest or mtime > newest[sid][0]:
            newest[sid] = (mtime, path)

    blocked: List[dict] = []
    for sid, (mtime, path) in newest.items():
        meta = read_rollout(path)
        if not meta:
            continue
        is_blocked, reset_epoch, detail = evaluate_block(meta["rate_limits"], threshold)
        if not is_blocked:
            continue
        blocked.append({
            "codex_id": sid,
            "cwd": meta.get("cwd"),
            "title": titles.get(sid),
            "status_detail": detail,
            "reset_epoch": reset_epoch,
            "rollout": path,
            "mtime": mtime,
        })
    return blocked
