"""MCP server exposing get_project_status(project) -> StatusReport.

Uses the standard MCP Python SDK (`pip install mcp`). The SDK is imported here
only, so the probe CLI works without it. Run:

    python -m status_mcp.server              # stdio transport (default)

Wire into an MCP client (Claude Desktop, etc.) — see README.md.
"""
from __future__ import annotations

import json

from .config import list_projects
from .report import build_status_report

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The MCP SDK is not installed. Run `pip install mcp` (see README.md).\n"
        f"Original import error: {e}"
    )

mcp = FastMCP("cos-console-status")


@mcp.tool()
def get_project_status(project: str) -> dict:
    """Assemble a real StatusReport for a project from this host's signals.

    Read-only. Returns the StatusReport contract (see SCHEMA.md): tickets,
    tests, deploy, visual_review, decisions, plus an `availability` map that
    says which sections are live vs unavailable — read it before asserting any
    number as fact.

    Args:
        project: project key, e.g. "dstrader". Use list_projects() for options.
    """
    try:
        return build_status_report(project)
    except KeyError as e:
        return {"error": str(e), "known_projects": list_projects()}


@mcp.tool()
def list_known_projects() -> list[str]:
    """List the project keys this server can report on."""
    return list_projects()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
