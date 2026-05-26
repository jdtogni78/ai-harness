"""Pure parsing + selection for Codex rollout sessions.

Codex writes one JSONL "rollout" per thread under ``~/.codex/sessions`` (and
``~/.codex/archived_sessions``). The clean conversation lives in the
``event_msg`` stream (``user_message`` / ``agent_message``); the parallel
``response_item`` stream additionally carries the model's reasoning and its
tool calls + outputs. We deliberately read only the former: replaying raw
tool_use/tool_result pairs is exactly what trips Claude Code's resume
validation, so dropping them keeps the converted session resume-valid.

Everything here is pure -- callers pass file *contents* (lists of lines), so the
parsing and selection logic is unit-testable without touching disk.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

# (role, text, timestamp); role is "user" | "assistant".
Turn = Tuple[str, str, Optional[str]]


class CodexSession(NamedTuple):
    id: str
    title: str
    cwd: str
    path: str
    updated_at: str   # ISO string from the index, else "" (sorts last)
    archived: bool


def parse_index(text: str) -> Dict[str, Dict[str, str]]:
    """``session_index.jsonl`` content -> ``{id: {title, updated_at}}``."""
    out: Dict[str, Dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        sid = r.get("id")
        if sid:
            out[sid] = {
                "title": r.get("thread_name", ""),
                "updated_at": r.get("updated_at", ""),
            }
    return out


def meta_of(lines: Iterable[str]) -> Tuple[str, str]:
    """Return ``(id, cwd)`` from a rollout's ``session_meta`` line.

    Only the first few lines need be passed; returns ``("", "")`` if none of
    them is the meta record.
    """
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") == "session_meta":
            p = r.get("payload", {})
            return p.get("id", ""), p.get("cwd", "")
    return "", ""


def _is_preamble(text: str) -> bool:
    # The first "user" message is the injected AGENTS.md / environment context,
    # not something the human typed -- skip it.
    return "<INSTRUCTIONS>" in text or "AGENTS.md instructions for" in text


def extract_turns(lines: Iterable[str]) -> List[Turn]:
    """Pull the clean user/agent conversation from a rollout's event stream."""
    turns: List[Turn] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "event_msg":
            continue
        payload = r.get("payload", {})
        kind = payload.get("type")
        ts = r.get("timestamp")
        if kind == "user_message":
            txt = payload.get("message", "")
            if isinstance(txt, list):
                txt = "".join(c.get("text", "") for c in txt if isinstance(c, dict))
            if not isinstance(txt, str) or _is_preamble(txt):
                continue
            txt = txt.strip()
            if txt:
                turns.append(("user", txt, ts))
        elif kind == "agent_message":
            txt = (payload.get("message") or "").strip()
            if txt:
                turns.append(("assistant", txt, ts))
    return turns


def select(
    sessions: Iterable[CodexSession],
    ids: Optional[Iterable[str]] = None,
    match: Optional[str] = None,
    cwd: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[CodexSession]:
    """Filter by id-prefix / title-regex / exact cwd, newest-first, capped.

    ``ids`` match a session whose id equals or starts with the given value (so a
    short prefix is enough). All supplied filters are AND-combined.
    """
    res = list(sessions)
    if ids:
        wanted = list(ids)
        res = [s for s in res if any(s.id == w or s.id.startswith(w) for w in wanted)]
    if match:
        rx = re.compile(match, re.IGNORECASE)
        res = [s for s in res if rx.search(s.title or "")]
    if cwd:
        res = [s for s in res if s.cwd == cwd]
    res.sort(key=lambda s: s.updated_at, reverse=True)
    if limit:
        res = res[:limit]
    return res
