"""Pure builders for Claude Code session JSONL.

No I/O: callers pass the parsed conversation turns and get back the ordered list
of record dicts that make up one resumable ``~/.claude/projects/<enc>/<uuid>.jsonl``
session. The cwd -> project-dir encoding mirrors how Claude Code itself names
project folders (every non-alphanumeric char becomes ``-``).
"""
from __future__ import annotations

import re
import uuid as _uuid
from typing import Callable, List, Optional, Tuple

Turn = Tuple[str, str, Optional[str]]  # (role, text, timestamp)

DEFAULT_VERSION = "2.1.142"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_BRANCH = "main"
FALLBACK_TS = "1970-01-01T00:00:00.000Z"


def encode_project_dir(cwd: str) -> str:
    """cwd -> the ``~/.claude/projects`` subdir name Claude Code uses for it.

    Claude Code replaces every non-alphanumeric character with ``-``, so
    ``/Users/x/dev/AppOne/.claude/worktrees/bridge-cse_01AB`` becomes
    ``-Users-x-dev-AppOne--claude-worktrees-bridge-cse-01AB``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def merge_turns(turns: List[Turn]) -> List[Turn]:
    """Merge consecutive same-role turns; drop empties and any leading non-user.

    A Claude session must strictly alternate user/assistant and open on a user
    turn, so this collapses Codex's multi-message turns (it can emit several
    ``agent_message`` events per turn) into one each.
    """
    merged: List[Turn] = []
    for role, text, ts in turns:
        text = (text or "").strip()
        if not text:
            continue
        if merged and merged[-1][0] == role:
            prev = merged[-1]
            merged[-1] = (prev[0], prev[1] + "\n\n" + text, prev[2])
        else:
            merged.append((role, text, ts))
    while merged and merged[0][0] != "user":
        merged.pop(0)
    return merged


def build_session(
    title: str,
    turns: List[Turn],
    cwd: str,
    *,
    session_id: Optional[str] = None,
    version: str = DEFAULT_VERSION,
    model: str = DEFAULT_MODEL,
    branch: str = DEFAULT_BRANCH,
    new_uuid: Optional[Callable[[], str]] = None,
    fallback_ts: str = FALLBACK_TS,
) -> Tuple[str, List[dict]]:
    """Build ``(session_id, records)`` for one Claude session.

    *turns* must already be merged (see :func:`merge_turns`). Records are ready
    to ``json.dumps`` one per line. Pure: inject *new_uuid* for deterministic
    tests; defaults to ``uuid4``.
    """
    gen = new_uuid or (lambda: str(_uuid.uuid4()))
    sid = session_id or gen()
    base = {
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "sdk-cli",
        "cwd": cwd,
        "sessionId": sid,
        "version": version,
        "gitBranch": branch,
    }
    records: List[dict] = [{"type": "ai-title", "aiTitle": title, "sessionId": sid}]
    parent: Optional[str] = None
    for role, text, ts in turns:
        node = gen()
        rec = dict(base)
        rec["parentUuid"] = parent
        rec["uuid"] = node
        rec["timestamp"] = ts or fallback_ts
        if role == "user":
            rec["type"] = "user"
            rec["message"] = {"role": "user", "content": text}
        else:
            rec["type"] = "assistant"
            rec["message"] = {
                "model": model,
                "id": "msg_" + gen().replace("-", "")[:24],
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            }
        records.append(rec)
        parent = node
    return sid, records
