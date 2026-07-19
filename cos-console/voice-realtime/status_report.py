"""StatusReport stub for the voice-realtime POC.

SHARED CONTRACT (PROJECT.md, SETTLED DECISION #3): every voice POC calls ONE
tool, ``get_project_status(project) -> StatusReport``. The data-plane worker
(w0-data-plane / status-mcp) OWNS the real schema; until it publishes, we stub
against the v0 shape verbatim. Do NOT diverge from this shape — when w0 ships
the real MCP, ``get_project_status`` gets repointed at it and the rest of the
agent is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# A couple of hand-authored fixtures so the demo has something to talk about.
# "dstrader" is the target demo project from the brief.
_FIXTURES: dict[str, dict] = {
    "dstrader": {
        "project": "dstrader",
        "tickets": {
            "todo": 5,
            "in_progress": 2,
            "done": 18,
            "blocked": 1,
            "items": [
                {"id": "DST-142", "title": "Backtest engine: slippage model", "state": "in_progress", "url": "https://example/DST-142"},
                {"id": "DST-137", "title": "Live order router reconnect", "state": "blocked", "url": "https://example/DST-137"},
                {"id": "DST-150", "title": "Portfolio risk dashboard", "state": "todo", "url": "https://example/DST-150"},
            ],
        },
        "tests": {"count": 412, "passing": 408, "failing": 4, "coverage_pct": 76.3, "last_run": _now()},
        "deploy": {"last_deployed_at": _now(), "env": "staging", "commit": "a1b2c3d", "status": "ok"},
        "visual_review": {"done": False, "artifacts": []},
        "decisions": [
            {"when": _now(), "summary": "Adopt event-sourced fills for auditability", "source": "close-work brief DST-142"},
        ],
        "open_questions": [
            "Is the slippage model validated against prod fills?",
            "DST-137 blocked on broker sandbox creds — who owns that?",
        ],
    },
    "cos-console": {
        "project": "cos-console",
        "tickets": {
            "todo": 3,
            "in_progress": 4,
            "done": 1,
            "blocked": 0,
            "items": [
                {"id": "COS-1", "title": "Wave 1: compare voice approaches", "state": "in_progress", "url": "https://example/COS-1"},
                {"id": "COS-2", "title": "StatusReport MCP (data plane)", "state": "in_progress", "url": "https://example/COS-2"},
            ],
        },
        "tests": {"count": 0, "passing": 0, "failing": 0, "coverage_pct": None, "last_run": None},
        "deploy": {"last_deployed_at": None, "env": "", "commit": "", "status": "unknown"},
        "visual_review": {"done": False, "artifacts": []},
        "decisions": [
            {"when": _now(), "summary": "voice-realtime uses GPT brain + Claude-as-tool (counterpoint)", "source": "PROJECT.md SETTLED DECISION #2"},
        ],
        "open_questions": ["Which voice approach wins on latency vs reasoning quality?"],
    },
}


def get_status_report(project: str) -> dict:
    """Return a v0 StatusReport dict for ``project``.

    Unknown projects get a well-formed empty report so the tool never errors —
    the voice model can then say "I don't have data on that project yet."
    """
    key = (project or "").strip().lower()
    if key in _FIXTURES:
        report = dict(_FIXTURES[key])
        report["generated_at"] = _now()
        return report

    return {
        "project": project,
        "generated_at": _now(),
        "tickets": {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0, "items": []},
        "tests": {"count": 0, "passing": 0, "failing": 0, "coverage_pct": None, "last_run": None},
        "deploy": {"last_deployed_at": None, "env": "", "commit": "", "status": "unknown"},
        "visual_review": {"done": False, "artifacts": []},
        "decisions": [],
        "open_questions": [],
        "_note": "no fixture for this project; w0-data-plane MCP will supply real data",
    }


def known_projects() -> list[str]:
    return sorted(_FIXTURES.keys())
