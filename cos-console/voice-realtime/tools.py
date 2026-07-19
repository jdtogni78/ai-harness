"""The single shared voice tool: get_project_status.

Both the live Realtime agent and the dry-run harness dispatch through
``handle_tool_call`` so behaviour is identical with or without audio/keys.
"""

from __future__ import annotations

import json

from claude_tool import summarize_status
from status_report import get_status_report, known_projects

# OpenAI Realtime tool definition (session.update -> tools[]).
GET_PROJECT_STATUS_TOOL = {
    "type": "function",
    "name": "get_project_status",
    "description": (
        "Get the current status of a software project (tickets, tests, deploys, "
        "decisions, open questions) as a spoken-ready summary. Call this whenever "
        "the operator asks how a project is doing, what's blocked, test/deploy "
        "state, or for a standup-style update. Known projects: "
        + ", ".join(known_projects())
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Project name, e.g. 'dstrader'.",
            },
            "question": {
                "type": "string",
                "description": "The operator's actual question, verbatim, so the "
                "reasoning tool can answer it specifically.",
            },
        },
        "required": ["project"],
    },
}


def handle_tool_call(name: str, arguments: dict) -> str:
    """Execute a tool call, returning a string for the model to speak from."""
    if name != "get_project_status":
        return json.dumps({"error": f"unknown tool {name}"})

    project = arguments.get("project", "")
    question = arguments.get("question") or f"status on {project}"
    report = get_status_report(project)
    # THE HANDOFF: route the raw report through Claude (or raw/mock per config).
    return summarize_status(question, report)
