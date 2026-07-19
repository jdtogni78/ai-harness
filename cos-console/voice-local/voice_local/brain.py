"""The brain: Claude with a single tool, `get_project_status(project)`.

This is the ONLY network hop in the whole pipeline. Everything else (mic, STT,
TTS, speaker) stays on-device. The brain is deliberately kept to one tool per the
shared status contract in PROJECT.md.
"""
from __future__ import annotations

import json
import time

from .config import CONFIG
from .status_stub import KNOWN_PROJECTS, get_project_status

SYSTEM_PROMPT = (
    "You are a voice-first chief-of-staff assistant. You REPORT and VALIDATE project "
    "status; you do not write code. You are spoken aloud by a local TTS engine, so:\n"
    "- Keep answers short and conversational — 1-3 sentences unless asked for detail.\n"
    "- Never read out URLs, IDs, or raw JSON. Summarize numbers in plain speech.\n"
    "- When asked about a project's status, tests, deploys, tickets, decisions, or open "
    "questions, call get_project_status and answer from it.\n"
    f"- Known projects you can look up: {', '.join(KNOWN_PROJECTS)}.\n"
    "- If data is missing (e.g. no tests run), say so plainly and flag it as a gap."
)

TOOLS = [
    {
        "name": "get_project_status",
        "description": (
            "Get the current engineering status for a project from the harness: "
            "tickets (todo/in-progress/done/blocked), test counts + coverage, last "
            "deploy, whether a visual review happened, recent decisions, and open "
            "questions. Call this whenever the user asks how a project is doing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": f"Project name, e.g. one of: {', '.join(KNOWN_PROJECTS)}",
                }
            },
            "required": ["project"],
        },
    }
]


class Brain:
    def __init__(self):
        self.history: list[dict] = []
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=CONFIG.anthropic_api_key)
        return self._client

    def reset(self) -> None:
        self.history = []

    def respond(self, user_text: str) -> tuple[str, dict]:
        """Run one turn (with tool loop). Returns (spoken_reply, timing_dict)."""
        if not CONFIG.has_key():
            raise RuntimeError("ANTHROPIC_API_KEY not set — brain needs the one network hop.")

        client = self._client_lazy()
        self.history.append({"role": "user", "content": user_text})
        timing = {"api_s": 0.0, "tool_calls": 0}

        while True:
            t0 = time.perf_counter()
            resp = client.messages.create(
                model=CONFIG.model,
                max_tokens=CONFIG.max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.history,
            )
            timing["api_s"] += time.perf_counter() - t0
            self.history.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use" and block.name == "get_project_status":
                        timing["tool_calls"] += 1
                        project = block.input.get("project", "")
                        status = get_project_status(project)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(status),
                        })
                self.history.append({"role": "user", "content": tool_results})
                continue

            # Final assistant turn: collect the spoken text.
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return text, timing
