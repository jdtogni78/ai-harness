"""Dataclasses + insert helpers for the corpus.

Each row type has a small dataclass for type-safe construction and a
get_or_insert helper that returns the row id. Insert helpers are idempotent
on natural keys (sha256 for cases, name+version for judges/scorers).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


VALID_VERDICTS = {"allow", "ask", "deny", "prompt"}
VALID_TIERS = {"green", "yellow", "orange", "red"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def case_sha256(tool: str, subject: str, cwd: str | None) -> str:
    """Stable dedup key. cwd included so the same command from different
    worktrees still counts as one situation only when truly identical."""
    h = hashlib.sha256()
    h.update(tool.encode("utf-8"))
    h.update(b"\x1f")
    h.update(subject.encode("utf-8"))
    h.update(b"\x1f")
    h.update((cwd or "").encode("utf-8"))
    return h.hexdigest()


@dataclass
class Case:
    ts: str
    source: str
    tool: str
    subject: str
    cwd: Optional[str] = None
    session_id: Optional[str] = None
    args_json: Optional[str] = None
    ctx_json: Optional[str] = None
    redacted: bool = False

    @property
    def sha256(self) -> str:
        return case_sha256(self.tool, self.subject, self.cwd)


@dataclass
class Judge:
    name: str
    version: str
    guideline_path: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None


@dataclass
class Verdict:
    case_id: int
    run_id: int
    verdict: str
    risk_tier: Optional[str] = None
    rationale: Optional[str] = None
    tier_used: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    enforced: Optional[bool] = None


def get_or_insert_case(conn: sqlite3.Connection, case: Case) -> tuple[int, bool]:
    """Returns (case_id, inserted_now). Idempotent on case.sha256."""
    sha = case.sha256
    row = conn.execute(
        "SELECT id FROM case_row WHERE sha256 = ?", (sha,)
    ).fetchone()
    if row is not None:
        return int(row["id"]), False
    cur = conn.execute(
        """
        INSERT INTO case_row
            (ts, source, tool, subject, cwd, session_id,
             args_json, ctx_json, redacted, sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case.ts, case.source, case.tool, case.subject, case.cwd,
            case.session_id, case.args_json, case.ctx_json,
            1 if case.redacted else 0, sha,
        ),
    )
    return int(cur.lastrowid), True


def get_or_insert_judge(conn: sqlite3.Connection, judge: Judge) -> int:
    row = conn.execute(
        "SELECT id FROM judge WHERE name = ? AND version = ?",
        (judge.name, judge.version),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO judge (name, version, guideline_path, system_prompt, model)
        VALUES (?, ?, ?, ?, ?)
        """,
        (judge.name, judge.version, judge.guideline_path,
         judge.system_prompt, judge.model),
    )
    return int(cur.lastrowid)


def get_or_insert_run(
    conn: sqlite3.Connection,
    judge_id: int,
    case_set: str,
    git_sha: str | None = None,
    notes: str | None = None,
    reuse_singleton: bool = False,
) -> int:
    """Create a new run row. If reuse_singleton=True, returns the most recent
    matching (judge_id, case_set) run instead — used for the import flow where
    we want a single rolling 'production-hook (imported)' run."""
    if reuse_singleton:
        row = conn.execute(
            """
            SELECT id FROM run
             WHERE judge_id = ? AND case_set = ?
             ORDER BY id DESC LIMIT 1
            """,
            (judge_id, case_set),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    cur = conn.execute(
        "INSERT INTO run (judge_id, case_set, ts, git_sha, notes) VALUES (?, ?, ?, ?, ?)",
        (judge_id, case_set, _now(), git_sha, notes),
    )
    return int(cur.lastrowid)


def insert_verdict(conn: sqlite3.Connection, verdict: Verdict) -> tuple[int, bool]:
    """Idempotent on (case_id, run_id). Returns (verdict_id, inserted_now)."""
    row = conn.execute(
        "SELECT id FROM verdict WHERE case_id = ? AND run_id = ?",
        (verdict.case_id, verdict.run_id),
    ).fetchone()
    if row is not None:
        return int(row["id"]), False
    cur = conn.execute(
        """
        INSERT INTO verdict
            (case_id, run_id, verdict, risk_tier, rationale, tier_used,
             model, latency_ms, cost_usd, enforced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verdict.case_id, verdict.run_id, verdict.verdict,
            verdict.risk_tier, verdict.rationale, verdict.tier_used,
            verdict.model, verdict.latency_ms, verdict.cost_usd,
            None if verdict.enforced is None else int(verdict.enforced),
        ),
    )
    return int(cur.lastrowid), True
