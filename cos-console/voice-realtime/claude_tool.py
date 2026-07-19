"""Claude-as-a-tool: the reasoning handoff.

This is the crux of Approach B. The OpenAI Realtime (GPT) model is the fast
conversational I/O; when it needs anything *substantive* it calls the
``get_project_status`` tool. That tool, here, routes the raw StatusReport
through **Claude** for the summary/reasoning, then hands a tight spoken-style
answer back to the Realtime model to voice.

Two routing modes (set via env / config so we can A/B the tradeoff described in
the brief):

  CLAUDE_ROUTING=claude  -> Claude summarizes the StatusReport (default).
                            Tests "GPT-brain + Claude-as-tool".
  CLAUDE_ROUTING=raw      -> return raw JSON, let the GPT brain reason alone.
                            Tests "GPT-brain only" -> shows where it's weaker.

If no ANTHROPIC_API_KEY is present, a deterministic MOCK summarizer stands in so
the dry-run path works with zero secrets.
"""

from __future__ import annotations

import json
import os

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

_SYSTEM = (
    "You are the reasoning core of a voice 'chief of staff'. You are called as a "
    "tool by a fast speech model. Given a project StatusReport (JSON), answer the "
    "operator's question in a SPOKEN, concise way: 2-4 sentences, no markdown, no "
    "lists, lead with the headline. Surface risk (failing tests, blocked tickets, "
    "unanswered questions) proactively. If the report is empty, say so plainly."
)


def _mock_summary(question: str, report: dict) -> str:
    """Deterministic, no-API fallback so the repo runs without keys."""
    proj = report.get("project", "unknown")
    t = report.get("tickets", {})
    tests = report.get("tests", {})
    if not t.get("items") and not tests.get("count"):
        return f"I don't have any real data on {proj} yet — the data plane MCP isn't wired up."

    parts = [
        f"On {proj}: {t.get('in_progress', 0)} in progress, "
        f"{t.get('blocked', 0)} blocked, {t.get('done', 0)} done."
    ]
    failing = tests.get("failing", 0)
    if failing:
        parts.append(
            f"Heads up — {failing} of {tests.get('count', 0)} tests are failing, "
            f"coverage {tests.get('coverage_pct')}%."
        )
    dep = report.get("deploy", {})
    if dep.get("status") and dep.get("status") != "unknown":
        parts.append(f"Last deploy to {dep.get('env')} was {dep.get('status')}.")
    oq = report.get("open_questions") or []
    if oq:
        parts.append(f"Open question: {oq[0]}")
    return " ".join(parts)


def summarize_status(question: str, report: dict, routing: str | None = None) -> str:
    """Route a StatusReport through Claude (or mock) and return spoken-style text.

    ``question`` is the operator's utterance / the GPT model's framing.
    ``routing`` overrides CLAUDE_ROUTING env for testing.
    """
    mode = (routing or os.environ.get("CLAUDE_ROUTING", "claude")).lower()

    if mode == "raw":
        # Hand raw JSON straight back; the GPT brain reasons over it unaided.
        return json.dumps(report)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _mock_summary(question, report)

    try:
        import anthropic
    except ImportError:
        return _mock_summary(question, report)

    client = anthropic.Anthropic(api_key=api_key)
    user = (
        f"Operator asked: {question!r}\n\n"
        f"StatusReport JSON:\n{json.dumps(report, indent=2)}\n\n"
        "Answer for the voice model to speak."
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in msg.content if block.type == "text").strip()
