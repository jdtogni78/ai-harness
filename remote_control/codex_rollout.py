"""Codex rollout parsing: read a ``~/.codex/sessions/**/rollout-*.jsonl`` log
and decide whether its session is blocked on a rate-limit window.

Each session is a JSONL log whose ``token_count`` events carry a ``rate_limits``
block, e.g.::

    {"limit_id":"codex","plan_type":"plus",
     "primary":   {"used_percent":90,"window_minutes":300,  "resets_at":1779433580},
     "secondary": {"used_percent":48,"window_minutes":10080,"resets_at":1779871017},
     "rate_limit_reached_type": null}

``primary`` is the rolling 5h window; ``secondary`` the weekly window. A session
is blocked when ``rate_limit_reached_type`` is set or a window's ``used_percent``
reaches the threshold.

These two pure parsers were the shared half of the (now-removed, #175)
``usage_limit/codex.py``: the inventory view reads them to flag a Codex session
as limit-paused. The codex auto-resume loop that also lived there was deprecated
along with the Claude usage-limit monitor.
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

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
