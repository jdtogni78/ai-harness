"""The single tool this POC exposes to Claude: get_project_status(project).

STUBS the tool against W0's **FROZEN StatusReport v1.0** contract
(`~/dev/cos-console/status-mcp/SCHEMA.md` + `status_report.schema.json`). W0 owns
the schema; this consumes it verbatim and does not diverge. When W0's live MCP is
wired, only `get_status_report()` / `STATUS_REPORTS` change.

Two guardrail rules from the contract that the voice layer MUST honor:
  * `availability[section]` ∈ {live, partial, unavailable} — read it before
    voicing any number.
  * Unreachable numerics are `null` (NOT `0`). `0` means "we looked, it's zero";
    `null` means "we couldn't look". The voice layer says "I don't have test
    data" instead of a fabricated "0 passing".

`cos-console` is included specifically to exercise the hedge: its `tests` and
`deploy` sections are `unavailable` with null numerics.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# StatusReport v1.0 stubs (shape frozen to SCHEMA.md — do not diverge).
# ---------------------------------------------------------------------------
STATUS_REPORTS: dict[str, dict[str, Any]] = {
    # Fully-live project — drives the main demo.
    "dstrader": {
        "schema_version": SCHEMA_VERSION,
        "project": "dstrader",
        "generated_at": "2026-07-18T09:15:00Z",
        "availability": {
            "tickets": "live",
            "tests": "live",
            "deploy": "live",
            "visual_review": "live",
            "decisions": "live",
        },
        "tickets": {
            "total": 18,
            "todo": 4,
            "in_progress": 2,
            "done": 11,
            "blocked": 1,
            "items": [
                {
                    "id": "142",
                    "title": "Backtest engine: slippage model",
                    "state": "In Progress",
                    "url": "https://github.com/jdtogni78/dstrader/issues/142",
                    "repo": "jdtogni78/dstrader",
                },
                {
                    "id": "138",
                    "title": "Live order router reconnect logic",
                    "state": "Blocked",
                    "url": "https://github.com/jdtogni78/dstrader/issues/138",
                    "repo": "jdtogni78/dstrader",
                },
            ],
            "source": "gh project item-list 1 (repo=jdtogni78/dstrader)",
        },
        "tests": {
            "available": True,
            "count": 214,
            "passing": 209,
            "failing": 5,
            "skipped": 0,
            "coverage_pct": 78.4,
            "last_run": "2026-07-18T08:52:00Z",
            "source": "target/surefire-reports + target/site/jacoco/jacoco.csv",
        },
        "deploy": {
            "last_deployed_at": "2026-07-17T22:03:00Z",
            "env": "dstrader-docker",
            "commit": "2f4bf29b53",
            "status": "ok",
            "duration_s": 2,
            "target": "dstrader-docker",
            "source": "deploy_logs/INDEX.md",
        },
        "visual_review": {
            "done": True,
            "artifacts": [
                {
                    "name": "investment-strategy.demo.yaml",
                    "kind": "demo_script",
                    "path": "demos/investment-strategy.demo.yaml",
                    "when": "2026-06-04T14:00:00Z",
                }
            ],
            "source": "demos",
        },
        "decisions": [
            {
                "when": "2026-07-16T14:00:00Z",
                "summary": "Adopt Cartesia for TTS in the voice console POC",
                "source": "close-work",
                "ref": "DST-131",
            }
        ],
        "open_questions": [
            "Should the slippage model be per-symbol or per-venue?",
        ],
        "warnings": [],
    },
    # Partially-reachable project — exercises the anti-fabrication guardrail:
    # tests and deploy are UNAVAILABLE (null numerics, not zeros).
    "cos-console": {
        "schema_version": SCHEMA_VERSION,
        "project": "cos-console",
        "generated_at": "2026-07-18T09:15:00Z",
        "availability": {
            "tickets": "live",
            "tests": "unavailable",
            "deploy": "unavailable",
            "visual_review": "partial",
            "decisions": "live",
        },
        "tickets": {
            "total": 7,
            "todo": 5,
            "in_progress": 2,
            "done": 0,
            "blocked": 0,
            "items": [],
            "source": "gh project item-list 2 (repo=jdtogni78/cos-console)",
        },
        "tests": {
            "available": False,
            "count": None,
            "passing": None,
            "failing": None,
            "skipped": None,
            "coverage_pct": None,
            "last_run": None,
            "source": None,
        },
        "deploy": {
            "last_deployed_at": None,
            "env": "",
            "commit": "",
            "status": "unknown",
            "duration_s": None,
            "target": None,
            "source": None,
        },
        "visual_review": {
            "done": True,
            "artifacts": [
                {
                    "name": "narrate-demo.mp4",
                    "kind": "video",
                    "path": "demos/narrate-demo.mp4",
                    "when": "2026-07-15T10:00:00Z",
                }
            ],
            "source": "demos",
        },
        "decisions": [
            {
                "when": "2026-07-18T00:00:00Z",
                "summary": "Freeze StatusReport contract at v1.0",
                "source": "handoff",
                "ref": "status-mcp/SCHEMA.md",
            }
        ],
        "open_questions": [
            "No test suite has run yet — run it?",
        ],
        "warnings": ["tests: no surefire/jacoco artifacts found", "deploy: no deploy log"],
    },
}


def _unavailable_report(project: str) -> dict[str, Any]:
    """Well-formed v1.0 report for an unknown project: everything unavailable,
    numerics null (never 0)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project or "unknown",
        # Assembly time is always known even when every signal is unreachable.
        "generated_at": "2026-07-18T09:15:00Z",
        "availability": {
            "tickets": "unavailable",
            "tests": "unavailable",
            "deploy": "unavailable",
            "visual_review": "unavailable",
            "decisions": "unavailable",
        },
        "tickets": {
            "total": None,
            "todo": 0,
            "in_progress": 0,
            "done": 0,
            "blocked": 0,
            "items": [],
        },
        "tests": {
            "available": False,
            "count": None,
            "passing": None,
            "failing": None,
            "skipped": None,
            "coverage_pct": None,
            "last_run": None,
            "source": None,
        },
        "deploy": {
            "last_deployed_at": None,
            "env": "",
            "commit": "",
            "status": "unknown",
            "duration_s": None,
            "target": None,
            "source": None,
        },
        "visual_review": {"done": False, "artifacts": [], "source": None},
        "decisions": [],
        "open_questions": [f"No project '{project}' in the registry."],
        "warnings": [f"unknown project: {project!r}"],
    }


def get_status_report(project: str) -> dict[str, Any]:
    """Return the (stub) StatusReport v1.0 for `project`.

    Unknown projects get a well-formed all-unavailable report so the voice loop
    degrades honestly instead of erroring.
    """
    key = (project or "").strip().lower()
    if key in STATUS_REPORTS:
        return STATUS_REPORTS[key]
    return _unavailable_report(key)


# ---------------------------------------------------------------------------
# Availability helpers — the honesty layer the voice code reads BEFORE voicing.
# ---------------------------------------------------------------------------
def section_reachable(report: dict[str, Any], section: str) -> bool:
    """True when a section's data may be voiced as fact.

    `unavailable` is never voiced. `partial`/`live` are voiced but the caller
    still guards individual `null` numerics.
    """
    return report.get("availability", {}).get(section) != "unavailable"


# ---------------------------------------------------------------------------
# Tool schema.
# ---------------------------------------------------------------------------
TOOL_NAME = "get_project_status"
TOOL_DESCRIPTION = (
    "Fetch the current engineering status for a project: ticket counts, test "
    "results and coverage, last deploy, whether a visual review happened, and "
    "recent decisions. The report includes an 'availability' map "
    "(live|partial|unavailable) per section, and numeric fields are null when a "
    "signal could not be collected. Call this whenever the operator asks how a "
    "project is doing, how many tests there are, whether it deployed, or what "
    "was decided."
)
TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "description": "The project name, e.g. 'dstrader'.",
        }
    },
    "required": ["project"],
    "additionalProperties": False,
}


def anthropic_tool_dict() -> dict[str, Any]:
    """Raw Anthropic Messages-API tool definition."""
    return {
        "name": TOOL_NAME,
        "description": TOOL_DESCRIPTION,
        "input_schema": TOOL_INPUT_SCHEMA,
    }
