"""Brain logic shared across the live and dry-run paths.

`SYSTEM_PROMPT` steers Claude (the real brain). `CannedBrain` is the keyless
fallback used by `--dry-run` when no ANTHROPIC_API_KEY is present.

Both honor W0's StatusReport v1.0 **anti-fabrication guardrail**: read the
`availability` map and treat `null` numerics as "couldn't look", never as `0`.
The chief-of-staff says "I don't have test data for X" instead of a fabricated
"0 passing" — this is a core validating-manager behavior, demoed in the POC.
"""

from __future__ import annotations

from typing import Any

from .status_tool import get_status_report, section_reachable

SYSTEM_PROMPT = (
    "You are a voice-first chief-of-staff console. You are spoken to and you "
    "speak back, so keep every reply SHORT and natural — one or two sentences, "
    "no markdown, no bullet lists, no URLs, no ISO timestamps read aloud.\n"
    "\n"
    "CRITICAL honesty rule: the get_project_status report carries an "
    "'availability' map (live|partial|unavailable) and uses null (not 0) for any "
    "number that could not be collected. NEVER voice a number from a section "
    "whose availability is 'unavailable' or whose value is null. Instead say you "
    "don't have that data, e.g. \"I don't have test data for cos-console yet.\" A "
    "null test count is NOT zero tests. Only assert numbers you can see are real.\n"
    "\n"
    "When the operator asks about a project's status, call get_project_status and "
    "give a concise spoken summary of the reachable facts: ticket counts, "
    "tests passing/total, whether it deployed, and any blocker — skipping or "
    "hedging any unavailable section. For a follow-up like 'how many tests?', "
    "answer with just the number and a word of context, or hedge if it's "
    "unavailable. Round percentages."
)


def _pct(v: Any) -> str:
    if v is None:
        return "coverage unknown"
    return f"{round(float(v))} percent coverage"


def _tests_phrase(report: dict[str, Any]) -> str | None:
    """A voiceable tests clause, or None if the data is unavailable/null."""
    tests = report["tests"]
    if not section_reachable(report, "tests") or not tests.get("available", True):
        return None
    if tests.get("count") is None or tests.get("passing") is None:
        return None
    clause = f"{tests['passing']} of {tests['count']} tests passing"
    if tests.get("failing"):
        clause += f", {tests['failing']} failing"
    clause += f", {_pct(tests.get('coverage_pct'))}"
    return clause + "."


def _tickets_phrase(report: dict[str, Any]) -> str | None:
    if not section_reachable(report, "tickets"):
        return None
    t = report["tickets"]
    clause = f"{t['done']} tickets done, {t['in_progress']} in progress, {t['todo']} to do"
    clause += f", and {t['blocked']} blocked." if t["blocked"] else "."
    return clause


def _deploy_phrase(report: dict[str, Any]) -> str | None:
    if not section_reachable(report, "deploy"):
        return None
    dep = report["deploy"]
    if dep["status"] == "ok" and dep["env"]:
        return f"Last deploy to {dep['env']} looks healthy."
    if dep["status"] == "failed":
        return "Heads up: the last deploy failed."
    return None


def spoken_status_summary(project: str) -> str:
    """Concise, TTS-friendly summary honoring availability/null."""
    r = get_status_report(project)
    name = r["project"]

    tickets = _tickets_phrase(r)
    tests = _tests_phrase(r)
    deploy = _deploy_phrase(r)

    parts = [f"Here's {name}."]
    if tests:
        parts.append(tests)
    else:
        parts.append("I don't have test data for it.")
    if tickets:
        parts.append(tickets)
    if deploy:
        parts.append(deploy)

    # Nothing reachable at all.
    if not tests and not tickets and not deploy:
        return f"I don't have any reachable status data for {name} right now."
    return " ".join(parts)


class CannedBrain:
    """Deterministic, keyless brain for dry-runs. Mirrors the guardrail."""

    def __init__(self) -> None:
        self._last_project: str | None = None

    def reply(self, utterance: str) -> str:
        text = (utterance or "").lower()
        project = self._extract_project(text) or self._last_project

        if "test" in text:
            return self._tests_reply(project)

        if "coverage" in text:
            return self._coverage_reply(project)

        if "deploy" in text or "prod" in text or "shipped" in text:
            return self._deploy_reply(project)

        if project:
            self._last_project = project
            return spoken_status_summary(project)
        return "Tell me which project you'd like a status on."

    # --- follow-ups (each reads availability before voicing a number) ---
    def _tests_reply(self, project: str | None) -> str:
        if not project:
            return "Which project?"
        self._last_project = project
        r = get_status_report(project)
        tests = r["tests"]
        if not section_reachable(r, "tests") or not tests.get("available", True) or tests.get("count") is None:
            return f"I don't have test data for {r['project']} yet."
        return (
            f"{r['project']} has {tests['count']} tests, "
            f"{tests['passing']} passing and {tests['failing']} failing."
        )

    def _coverage_reply(self, project: str | None) -> str:
        if not project:
            return "Which project?"
        self._last_project = project
        r = get_status_report(project)
        cov = r["tests"].get("coverage_pct")
        if not section_reachable(r, "tests") or cov is None:
            return f"I don't have coverage data for {r['project']}."
        return f"{r['project']} is at {round(cov)} percent coverage."

    def _deploy_reply(self, project: str | None) -> str:
        if not project:
            return "Which project?"
        self._last_project = project
        r = get_status_report(project)
        dep = r["deploy"]
        if not section_reachable(r, "deploy") or not dep["env"]:
            return f"I don't have deploy data for {r['project']}."
        return f"{r['project']} last deployed to {dep['env']} and the status is {dep['status']}."

    def _extract_project(self, text: str) -> str | None:
        from .status_tool import STATUS_REPORTS

        for name in STATUS_REPORTS:
            if name in text:
                return name
        return None
