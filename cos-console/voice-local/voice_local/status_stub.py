"""StatusReport stub.

W0 (status-mcp) OWNS the real StatusReport schema + MCP. Until it publishes, we
return canned data matching the v0 shape in PROJECT.md verbatim. Do NOT diverge
from that shape here — when the real MCP lands, swap `get_project_status` to call
it and delete this file.
"""
from __future__ import annotations

from datetime import datetime, timezone

# A tiny fake "harness" of a couple of projects so demos have something to say.
_FAKE_DB = {
    "dstrader": {
        "tickets": {
            "todo": 4, "in_progress": 2, "done": 11, "blocked": 1,
            "items": [
                {"id": "DST-42", "title": "Backtest engine slippage model", "state": "in_progress", "url": "https://example/dst-42"},
                {"id": "DST-47", "title": "Live order reconciliation", "state": "blocked", "url": "https://example/dst-47"},
            ],
        },
        "tests": {"count": 318, "passing": 314, "failing": 4, "coverage_pct": 76.2,
                  "last_run": "2026-07-17T22:14:00Z"},
        "deploy": {"last_deployed_at": "2026-07-16T09:02:00Z", "env": "prod",
                   "commit": "a1b2c3d", "status": "ok"},
        "visual_review": {"done": True, "artifacts": ["dashboard-2026-07-16.png"]},
        "decisions": [
            {"when": "2026-07-15T14:00:00Z", "summary": "Adopt event-sourced fills ledger", "source": "close-work brief #DST-40"},
        ],
        "open_questions": ["Is the slippage model validated against live prod fills yet?"],
    },
    "cos-console": {
        "tickets": {
            "todo": 6, "in_progress": 4, "done": 2, "blocked": 0,
            "items": [
                {"id": "COS-3", "title": "Wave 1: compare voice approaches", "state": "in_progress", "url": "https://example/cos-3"},
            ],
        },
        "tests": {"count": 0, "passing": 0, "failing": 0, "coverage_pct": None, "last_run": None},
        "deploy": {"last_deployed_at": None, "env": "", "commit": "", "status": "unknown"},
        "visual_review": {"done": False, "artifacts": []},
        "decisions": [
            {"when": "2026-07-18T10:00:00Z", "summary": "Four parallel voice POCs, Claude as brain", "source": "PROJECT.md"},
        ],
        "open_questions": ["Which voice approach wins on latency vs naturalness vs privacy?"],
    },
}


def get_project_status(project: str) -> dict:
    """Return a v0 StatusReport for `project` (stubbed).

    Signature/return shape are the shared contract — keep identical to the real MCP.
    """
    key = (project or "").strip().lower()
    data = _FAKE_DB.get(key)
    now = datetime.now(timezone.utc).isoformat()
    if data is None:
        # Unknown project: still return a valid, empty StatusReport (never raise at the brain).
        return {
            "project": project,
            "generated_at": now,
            "tickets": {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0, "items": []},
            "tests": {"count": 0, "passing": 0, "failing": 0, "coverage_pct": None, "last_run": None},
            "deploy": {"last_deployed_at": None, "env": "", "commit": "", "status": "unknown"},
            "visual_review": {"done": False, "artifacts": []},
            "decisions": [],
            "open_questions": [f"No harness data found for project '{project}'."],
        }
    return {"project": key, "generated_at": now, **data}


KNOWN_PROJECTS = sorted(_FAKE_DB.keys())
