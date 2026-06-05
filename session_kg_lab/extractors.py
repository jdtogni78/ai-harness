"""Tier-0 regex entity + relation extractors.

The cwd-on-the-session row already encodes the repo deterministically;
extractors here pull the *mentioned* entities out of user-turn text, assistant
text, and tool-call arguments. No LLM in this layer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import EntityRef, ExtractionResult, RelationRef


_TICKET_RE   = re.compile(r"(?:(?P<org>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+))?#(?P<num>\d{1,5})\b")
_CSE_RE      = re.compile(r"\bcse_[A-Za-z0-9]{16,}\b")
_SHA_RE      = re.compile(r"\b(?P<sha>[0-9a-f]{7,40})\b")  # short or full
_FILE_RE     = re.compile(r"(?:^|[\s\"'`(\[])(?P<p>(?:/|~?/|\.{1,2}/)?[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]{1,6})\b")
_ERROR_RE    = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Warning))\b")


def repo_from_cwd(cwd: str | None) -> str | None:
    """Pull the repo name out of the session cwd, ignoring worktree suffixes.

    `/Users/u/dev/ai-harness/.claude/worktrees/bridge-cse_…` -> `ai-harness`.
    """
    if not cwd:
        return None
    parts = Path(cwd).parts
    if "dev" in parts:
        i = parts.index("dev")
        if i + 1 < len(parts):
            return parts[i + 1]
    return Path(cwd).name or None


def _add(out: ExtractionResult, ent: EntityRef) -> None:
    out.entities.append(ent)


def _link(
    out: ExtractionResult,
    session_ent: EntityRef,
    target: EntityRef,
    rel_type: str,
    turn_idx: int | None = None,
) -> None:
    out.relations.append(RelationRef(src=session_ent, dst=target, rel_type=rel_type, turn_idx=turn_idx))


def extract_from_text(
    text: str,
    session_ent: EntityRef,
    turn_idx: int | None,
    out: ExtractionResult,
) -> None:
    """Run all regex extractors against one turn's text."""
    if not text:
        return

    for m in _TICKET_RE.finditer(text):
        org = m.group("org")
        repo = m.group("repo")
        num = m.group("num")
        if org and repo:
            canonical = f"{org}/{repo}#{num}"
        else:
            canonical = f"#{num}"
        ent = EntityRef(type="ticket", canonical=canonical, name=canonical)
        _add(out, ent)
        _link(out, session_ent, ent, "mentioned", turn_idx)

    for m in _CSE_RE.finditer(text):
        cid = m.group(0)
        ent = EntityRef(type="cse", canonical=cid, name=cid)
        _add(out, ent)
        _link(out, session_ent, ent, "mentioned", turn_idx)

    for m in _SHA_RE.finditer(text):
        sha = m.group("sha")
        if len(sha) < 7:
            continue
        ent = EntityRef(type="commit", canonical=sha[:40], name=sha[:12])
        _add(out, ent)
        _link(out, session_ent, ent, "mentioned", turn_idx)

    for m in _FILE_RE.finditer(text):
        p = m.group("p")
        if p.count("/") < 1 or p.startswith("//"):
            continue
        ent = EntityRef(type="file", canonical=p, name=Path(p).name)
        _add(out, ent)
        _link(out, session_ent, ent, "mentioned", turn_idx)

    for m in _ERROR_RE.finditer(text):
        name = m.group(1)
        ent = EntityRef(type="error_class", canonical=name, name=name)
        _add(out, ent)
        _link(out, session_ent, ent, "hit", turn_idx)


def extract_from_tool_call(
    tool_name: str,
    tool_input: dict,
    session_ent: EntityRef,
    turn_idx: int | None,
    out: ExtractionResult,
) -> None:
    """File-aware extraction for the structured tool-use inputs."""
    tool_ent = EntityRef(type="tool", canonical=tool_name, name=tool_name)
    _add(out, tool_ent)
    _link(out, session_ent, tool_ent, "used", turn_idx)

    for key in ("file_path", "path", "notebook_path"):
        p = tool_input.get(key) if isinstance(tool_input, dict) else None
        if isinstance(p, str) and p:
            ent = EntityRef(type="file", canonical=p, name=Path(p).name)
            _add(out, ent)
            rel = "touched" if tool_name in {"Edit", "Write", "NotebookEdit"} else "read"
            _link(out, session_ent, ent, rel, turn_idx)


def extract_repo_relation(
    repo: str | None,
    session_ent: EntityRef,
    out: ExtractionResult,
) -> None:
    if not repo:
        return
    ent = EntityRef(type="repo", canonical=repo, name=repo)
    _add(out, ent)
    _link(out, session_ent, ent, "worked_in", None)
