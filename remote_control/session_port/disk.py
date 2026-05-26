"""Disk loading of Codex rollout sessions (the one place that globs ~/.codex).

Codex writes one JSONL "rollout" per thread under ``~/.codex/sessions`` (and
``~/.codex/archived_sessions``), plus a ``session_index.jsonl`` carrying each
thread's title + ``updated_at``. This reads that on-disk layout into the pure
:class:`~remote_control.session_port.codex.CodexSession` records that the rest of
the package (and the unified :mod:`remote_control.inventory`) consumes.

The parsing itself is pure (:mod:`.codex`); this module is the thin I/O shell so
both ``codex-import`` and the inventory share one loader instead of each globbing
``~/.codex`` their own way.
"""
from __future__ import annotations

import glob
from pathlib import Path
from typing import List

from . import codex as cx

CODEX_DIR = Path.home() / ".codex"
_META_SCAN_LINES = 5  # session_meta is line 1, but scan a few for robustness


def find_rollouts(include_archived: bool, codex_dir: Path = CODEX_DIR) -> List[str]:
    """Sorted paths of every rollout JSONL (optionally incl. archived ones)."""
    paths = glob.glob(str(codex_dir / "sessions" / "**" / "*.jsonl"), recursive=True)
    if include_archived:
        paths += glob.glob(
            str(codex_dir / "archived_sessions" / "**" / "*.jsonl"), recursive=True
        )
    return sorted(paths)


def load_sessions(include_archived: bool, codex_dir: Path = CODEX_DIR
                  ) -> List[cx.CodexSession]:
    """Read the rollout dir into :class:`CodexSession` records.

    Each session's id + cwd come from its rollout ``session_meta`` line; its
    title + ``updated_at`` come from ``session_index.jsonl`` (empty when the
    index has no entry for it)."""
    idx_path = codex_dir / "session_index.jsonl"
    index = cx.parse_index(idx_path.read_text()) if idx_path.exists() else {}
    sessions: List[cx.CodexSession] = []
    for path in find_rollouts(include_archived, codex_dir):
        with open(path) as fh:
            head = [next(fh, "") for _ in range(_META_SCAN_LINES)]
        sid, cwd = cx.meta_of(head)
        if not sid:
            continue
        meta = index.get(sid, {})
        sessions.append(
            cx.CodexSession(
                id=sid,
                title=meta.get("title", ""),
                cwd=cwd,
                path=path,
                updated_at=meta.get("updated_at", ""),
                archived=("archived_sessions" in path),
            )
        )
    return sessions
