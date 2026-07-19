"""Assemble a StatusReport (SCHEMA.md v1.0) from the collectors."""
from __future__ import annotations

from datetime import datetime, timezone

from . import SCHEMA_VERSION
from . import collectors as C
from .config import ProjectConfig, get_project


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_status_report(project: str) -> dict:
    """Return a schema-valid StatusReport dict for `project`.

    Raises KeyError for an unknown project key; individual signal failures
    degrade gracefully (availability=unavailable + warnings), never raise.
    """
    cfg: ProjectConfig = get_project(project)
    warnings: list[str] = []

    tickets, av_tickets, w = C.collect_tickets(cfg); warnings += w
    tests, av_tests, w = C.collect_tests(cfg); warnings += w
    deploy, av_deploy, w = C.collect_deploy(cfg); warnings += w
    visual, av_visual, w = C.collect_visual_review(cfg); warnings += w
    decisions, av_decisions, w = C.collect_decisions(cfg); warnings += w

    open_questions = _derive_open_questions(tickets, tests, deploy, visual)

    return {
        "schema_version": SCHEMA_VERSION,
        "project": cfg.key,
        "generated_at": _now_z(),
        "availability": {
            "tickets": av_tickets,
            "tests": av_tests,
            "deploy": av_deploy,
            "visual_review": av_visual,
            "decisions": av_decisions,
        },
        "tickets": tickets,
        "tests": tests,
        "deploy": deploy,
        "visual_review": visual,
        "decisions": decisions,
        "open_questions": open_questions,
        "warnings": warnings,
    }


def _derive_open_questions(tickets, tests, deploy, visual) -> list[str]:
    """Chief-of-staff prompts the operator might want voiced back."""
    qs = []
    if not tests.get("available"):
        qs.append("No test artifacts were found — has the suite been run recently?")
    elif tests.get("failing"):
        qs.append(f"{tests['failing']} test(s) failing — investigate before deploy?")
    if deploy.get("status") == "failed":
        qs.append("Last deploy is marked FAILED — needs attention.")
    if not visual.get("done"):
        qs.append("No visual-review artifact found — should we record a demo?")
    if tickets.get("blocked"):
        qs.append(f"{tickets['blocked']} ticket(s) are BLOCKED.")
    return qs
