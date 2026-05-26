"""Pure detection / parsing / selection for the usage-limit monitor.

No network, no clock-of-its-own (callers pass ``now``), no logging -- so the
detection filter, reset-time parsing, backoff math, and resume-target selection
are all unit-testable against fixtures.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Sequence, Tuple

# Matches the cloud refusal detail for both the org/monthly usage limit and the
# rolling 5h session limit.
LIMIT_RE = re.compile(r"(usage limit|session limit)", re.I)
# "resets 7:50pm UTC" / "resets 7:50pm (UTC)" -> next occurrence of that time.
RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m\s*\(?utc\)?", re.I)


def post_turn_summary(session: dict) -> dict:
    """Present both at the top level (detail GET) and under external_metadata
    (list GET); prefer whichever is populated."""
    pts = session.get("post_turn_summary")
    if pts:
        return pts
    return (session.get("external_metadata") or {}).get("post_turn_summary") or {}


def limit_pause_detail(session: dict) -> Optional[str]:
    """The limit status_detail if this session is paused on a usage/session
    limit and is a safe resume candidate, else None."""
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


def parse_reset_epoch(detail: str, now: Optional[datetime] = None) -> Optional[float]:
    """Epoch of the next occurrence of a 'resets H:MMam/pm UTC' time, or None."""
    m = RESET_RE.search(detail or "")
    if not m:
        return None
    now = now or datetime.now(timezone.utc)
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    minute = int(m.group(2) or 0)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def compute_backoff(
    detail: str,
    attempts: int,
    now: float,
    backoffs_secs: Sequence[int],
) -> Tuple[float, str]:
    """Next-attempt epoch + a log line. If the detail names a reset time, wait
    until just after it; else exponential-ish backoff capped at the last step
    (so a persistent monthly limit just retries every 30 min)."""
    reset = parse_reset_epoch(detail)
    if reset:
        when = datetime.fromtimestamp(reset, timezone.utc)
        return reset + 30, f"  next attempt at reset {when:%Y-%m-%d %H:%M}Z"
    idx = min(max(attempts - 1, 0), len(backoffs_secs) - 1)
    delay = backoffs_secs[idx]
    return now + delay, f"  next attempt in {delay}s"


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


def pick_resume_target(
    sessions: Dict[str, dict],
    now: float,
    max_attempts: int,
) -> Optional[dict]:
    """Oldest-first eligible tracked session whose backoff has elapsed and which
    is under the attempt cap (0 = unlimited)."""
    eligible = [
        s for s in sessions.values()
        if (s.get("next_attempt_at") or 0) <= now
        and (max_attempts == 0 or s.get("attempts", 0) < max_attempts)
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda s: s.get("first_seen", ""))
    return eligible[0]
