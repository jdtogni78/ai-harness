"""Import the production perm_gate hook's existing decision log into the
lab corpus.

The hook writes one JSONL record per evaluation to
~/dev/ai-harness/logs/perm-gate-decisions.jsonl with this shape:

    {at, session_id, cwd, tool, subject, decision, risk, tier, reason, enforced}

Each record is one (case, verdict) pair: the case is the situation the hook
saw, and the verdict is whatever the live production hook decided for it.

All imported verdicts attach to a single rolling 'production-hook (imported)'
judge + run so we can use them as the baseline for future judge comparisons.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import models
from .redact import redact

DEFAULT_HOOK_LOG = Path.home() / "dev" / "ai-harness" / "logs" / "perm-gate-decisions.jsonl"

PRODUCTION_JUDGE_NAME = "production-hook"
PRODUCTION_JUDGE_VERSION = "imported"
PRODUCTION_CASE_SET = "hook-log"


def _source_from_tier(tier: str | None) -> str:
    """Map the hook's `tier` field to a corpus `source` value.

    Hook tier values seen in the wild: static-readonly, static-deny,
    static-ask, static-allow, ai, ai-error, no-rule, disabled, no-subject.
    """
    if not tier:
        return "hook_unknown"
    if tier.startswith("static-"):
        return "hook_static"
    if tier == "ai" or tier == "ai-error":
        return "hook_ai"
    if tier == "no-rule":
        # The hook saw no rule (AI tier off) — still a hook decision, just
        # one that fell through to the default. Worth keeping for corpus.
        return "hook_norule"
    return f"hook_{tier}"


@dataclass
class ImportStats:
    records_read: int = 0
    records_skipped: int = 0
    cases_inserted: int = 0
    cases_existing: int = 0
    verdicts_inserted: int = 0
    verdicts_existing: int = 0
    redacted_count: int = 0


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def import_hook_log(
    conn: sqlite3.Connection,
    path: Path | None = None,
) -> ImportStats:
    """Idempotent: re-running on the same file is a no-op."""
    src = path or DEFAULT_HOOK_LOG
    stats = ImportStats()

    judge_id = models.get_or_insert_judge(
        conn,
        models.Judge(
            name=PRODUCTION_JUDGE_NAME,
            version=PRODUCTION_JUDGE_VERSION,
            guideline_path="docs/session-manager-cases.md",
            model="static+haiku-4.5",
        ),
    )
    run_id = models.get_or_insert_run(
        conn,
        judge_id=judge_id,
        case_set=PRODUCTION_CASE_SET,
        notes=f"imported from {src}",
        reuse_singleton=True,
    )

    for rec in _iter_jsonl(src):
        stats.records_read += 1
        subject = rec.get("subject") or ""
        tool = rec.get("tool") or ""
        if not tool or not subject:
            stats.records_skipped += 1
            continue

        subj_r, subj_hit = redact(subject)
        reason_r, reason_hit = redact(rec.get("reason"))
        was_redacted = subj_hit or reason_hit
        if was_redacted:
            stats.redacted_count += 1

        case = models.Case(
            ts=rec.get("at") or "",
            source=_source_from_tier(rec.get("tier")),
            tool=tool,
            subject=subj_r or "",
            cwd=rec.get("cwd"),
            session_id=rec.get("session_id"),
            redacted=was_redacted,
        )
        case_id, case_new = models.get_or_insert_case(conn, case)
        if case_new:
            stats.cases_inserted += 1
        else:
            stats.cases_existing += 1

        decision = rec.get("decision") or "unknown"
        verdict = models.Verdict(
            case_id=case_id,
            run_id=run_id,
            verdict=decision,
            risk_tier=rec.get("risk"),
            rationale=reason_r,
            tier_used=rec.get("tier"),
            enforced=bool(rec.get("enforced")),
        )
        _, v_new = models.insert_verdict(conn, verdict)
        if v_new:
            stats.verdicts_inserted += 1
        else:
            stats.verdicts_existing += 1

    conn.commit()
    return stats
