"""Walk ~/.claude/projects/*/sessions/*.jsonl and populate the KG.

Phase 1: deterministic only. No LLM, no embeddings. Idempotent on
sha256(jsonl_path + last_mtime); re-runs on an unchanged corpus insert zero
new rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import extractors, redact
from .models import (
    EntityRef,
    ExtractionResult,
    IngestStats,
    SessionRow,
)


DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha_for_file(path: Path) -> str:
    st = path.stat()
    return hashlib.sha256(f"{path}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _scan_session(path: Path) -> tuple[SessionRow, list[dict]]:
    """First pass: extract SessionRow fields + collect rows for entity pass."""
    cwd = branch = title = model = cse_id = None
    started_at = ended_at = None
    ended_reason = None
    turn_count = 0
    rows: list[dict] = []

    for obj in _iter_jsonl(path):
        rows.append(obj)
        ts = obj.get("timestamp")
        if ts:
            if started_at is None:
                started_at = ts
            ended_at = ts
        if cwd is None and obj.get("cwd"):
            cwd = obj["cwd"]
        if branch is None and obj.get("gitBranch"):
            branch = obj["gitBranch"]
        if cse_id is None and isinstance(obj.get("sessionId"), str):
            sid = obj["sessionId"]
            if sid.startswith("cse_"):
                cse_id = sid
        msg = obj.get("message") or {}
        if isinstance(msg, dict):
            if model is None and msg.get("model"):
                model = msg["model"]
            if msg.get("role") in {"user", "assistant"}:
                turn_count += 1
        op = obj.get("operation")
        if op in {"close", "archive", "abort"}:
            ended_reason = op

    sha = _sha_for_file(path)
    host = os.environ.get("REMOTE_CONTROL_HOST") or socket.gethostname().split(".")[0]
    repo = extractors.repo_from_cwd(cwd)
    return (
        SessionRow(
            cse_id=cse_id,
            jsonl_path=str(path),
            sha256=sha,
            host=host,
            cwd=cwd,
            repo=repo,
            branch=branch,
            title=title,
            model=model,
            started_at=started_at,
            ended_at=ended_at,
            ended_reason=ended_reason,
            turn_count=turn_count,
            redacted=False,
        ),
        rows,
    )


def _text_of_message(msg) -> str:
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif part.get("type") == "tool_result" and isinstance(part.get("content"), str):
                    out.append(part["content"])
        return "\n".join(out)
    return ""


def _extract_entities(
    rows: list[dict],
    session_ent: EntityRef,
    repo: str | None,
) -> tuple[ExtractionResult, bool]:
    out = ExtractionResult()
    extractors.extract_repo_relation(repo, session_ent, out)
    any_redaction = False

    for turn_idx, obj in enumerate(rows):
        msg = obj.get("message") or {}
        text = _text_of_message(msg)
        red_text, hit = redact.redact(text)
        if hit:
            any_redaction = True
        if red_text:
            extractors.extract_from_text(red_text, session_ent, turn_idx, out)

        # Tool-use messages carry structured args
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    extractors.extract_from_tool_call(
                        part.get("name", "unknown"),
                        part.get("input") or {},
                        session_ent,
                        turn_idx,
                        out,
                    )
    return out, any_redaction


def _upsert_entity(conn: sqlite3.Connection, ent: EntityRef, when: str) -> int:
    row = conn.execute(
        "SELECT id FROM entity WHERE type = ? AND canonical = ?",
        (ent.type, ent.canonical),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE entity SET last_seen = ? WHERE id = ?", (when, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO entity (type, canonical, name, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (ent.type, ent.canonical, ent.name, when, when),
    )
    return cur.lastrowid


def _persist(
    conn: sqlite3.Connection,
    sess: SessionRow,
    extraction: ExtractionResult,
    stats: IngestStats,
) -> None:
    now = _now()
    existing = conn.execute(
        "SELECT id, sha256 FROM session WHERE jsonl_path = ?",
        (sess.jsonl_path,),
    ).fetchone()
    if existing and existing["sha256"] == sess.sha256:
        stats.sessions_existing += 1
        return

    if existing:
        conn.execute(
            "UPDATE session SET sha256=?, host=?, cwd=?, repo=?, branch=?, "
            "title=?, model=?, started_at=?, ended_at=?, ended_reason=?, "
            "turn_count=?, redacted=?, ingested_at=? WHERE id = ?",
            (
                sess.sha256, sess.host, sess.cwd, sess.repo, sess.branch,
                sess.title, sess.model, sess.started_at, sess.ended_at,
                sess.ended_reason, sess.turn_count, int(sess.redacted), now,
                existing["id"],
            ),
        )
        session_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO session (cse_id, jsonl_path, sha256, host, cwd, repo, "
            "branch, title, model, started_at, ended_at, ended_reason, "
            "turn_count, redacted, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sess.cse_id, sess.jsonl_path, sess.sha256, sess.host, sess.cwd,
                sess.repo, sess.branch, sess.title, sess.model, sess.started_at,
                sess.ended_at, sess.ended_reason, sess.turn_count,
                int(sess.redacted), now,
            ),
        )
        session_id = cur.lastrowid
        stats.sessions_inserted += 1

    # de-dupe entities within this extraction
    ent_ids: dict[tuple[str, str], int] = {}
    for ent in extraction.entities:
        key = (ent.type, ent.canonical)
        if key in ent_ids:
            continue
        eid = _upsert_entity(conn, ent, now)
        ent_ids[key] = eid
        stats.entities_inserted += 1

    # The session itself doesn't have an entity row (it's a session row);
    # relations reference dst by (type, id).  We map src "session" → -session_id
    # via a synthetic entity type "session" so the schema stays uniform.
    sess_ent_id = _upsert_entity(
        conn,
        EntityRef(type="session", canonical=str(session_id), name=sess.cse_id or sess.jsonl_path),
        now,
    )
    ent_ids[("session", str(session_id))] = sess_ent_id

    for rel in extraction.relations:
        src_key = (rel.src.type, rel.src.canonical)
        dst_key = (rel.dst.type, rel.dst.canonical)
        if src_key not in ent_ids:
            ent_ids[src_key] = _upsert_entity(conn, rel.src, now)
        if dst_key not in ent_ids:
            ent_ids[dst_key] = _upsert_entity(conn, rel.dst, now)
        try:
            conn.execute(
                "INSERT INTO relation (src_type, src_id, dst_type, dst_id, "
                "rel_type, session_id, turn_idx, weight) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    rel.src.type, ent_ids[src_key],
                    rel.dst.type, ent_ids[dst_key],
                    rel.rel_type, session_id, rel.turn_idx, rel.weight,
                ),
            )
            stats.relations_inserted += 1
        except sqlite3.IntegrityError:
            pass


def ingest(
    conn: sqlite3.Connection,
    root: Path | None = None,
    host_filter: str | None = None,
) -> IngestStats:
    root = root or DEFAULT_PROJECTS_ROOT
    stats = IngestStats()
    if not root.exists():
        return stats
    for jsonl_path in sorted(root.rglob("*.jsonl")):
        stats.files_seen += 1
        try:
            sess, rows = _scan_session(jsonl_path)
        except OSError:
            stats.files_skipped += 1
            continue
        if host_filter and sess.host != host_filter:
            stats.files_skipped += 1
            continue
        session_ent = EntityRef(
            type="session",
            canonical=sess.cse_id or sess.jsonl_path,
            name=sess.cse_id or jsonl_path.name,
        )
        extraction, any_red = _extract_entities(rows, session_ent, sess.repo)
        sess.redacted = any_red
        if any_red:
            stats.redacted_count += 1
        _persist(conn, sess, extraction, stats)
        conn.commit()
    return stats
