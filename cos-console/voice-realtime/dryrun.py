"""Transcript-only dry-run of the voice loop — NO audio, NO API key required.

This mocks the Realtime (GPT) brain's decision to call the shared tool, so you
can inspect the full loop and the Claude-as-tool handoff without secrets or a
microphone. It exercises the *real* status stub and the *real* Claude routing
module (which itself falls back to a mock summarizer when ANTHROPIC_API_KEY is
absent), so the plumbing under test is identical to the live path.

Usage:
    python main.py dryrun                      # scripted demo turns
    python main.py dryrun --ask "status on dstrader"
"""

from __future__ import annotations

import json
import re

from status_report import known_projects
from tools import handle_tool_call

# Naive intent detection standing in for the GPT brain's tool-call decision.
_STATUS_RE = re.compile(r"\b(status|how('?s| is)|standup|update|blocked|tests?|deploy)\b", re.I)


# The real Realtime model keeps conversation context; the mock brain fakes that
# with a sticky "last project talked about" so follow-ups like "anything blocked?"
# resolve the way the live agent would.
_last_project: str | None = None


def _mock_brain_decides(utterance: str) -> dict | None:
    """Return a tool call the GPT brain *would* make, or None for smalltalk."""
    global _last_project
    if not _STATUS_RE.search(utterance):
        return None
    project = ""
    for p in known_projects():
        if p in utterance.lower():
            project = p
            break
    if not project:
        m = re.search(r"on\s+([\w-]+)", utterance.lower())
        project = m.group(1) if m else (_last_project or "")
    if project:
        _last_project = project
    return {"name": "get_project_status", "arguments": {"project": project, "question": utterance}}


def run_turn(utterance: str) -> None:
    print(f"\n\033[1m🎙  operator:\033[0m {utterance}")
    call = _mock_brain_decides(utterance)
    if call is None:
        print("\033[2m   [gpt-brain] no tool needed — would answer conversationally\033[0m")
        print("\033[1m🔊 agent:\033[0m (smalltalk handled by the fast model directly)")
        return

    print(f"\033[2m   [gpt-brain] -> tool call {call['name']}({json.dumps(call['arguments'])})\033[0m")
    spoken = handle_tool_call(call["name"], call["arguments"])
    print(f"\033[2m   [claude-as-tool] returned {len(spoken)} chars\033[0m")
    print(f"\033[1m🔊 agent:\033[0m {spoken}")


DEMO_TURNS = [
    "Hey, what's the status on dstrader?",
    "Anything blocked?",
    "How's cos-console doing?",
    "What's the weather like?",  # smalltalk -> no tool
    "Give me a standup on some-unknown-project",
]


def main(ask: str | None = None) -> None:
    print("=== voice-realtime DRY RUN (transcript-only, no audio/keys) ===")
    turns = [ask] if ask else DEMO_TURNS
    for t in turns:
        run_turn(t)
    print("\n=== end dry run ===")
