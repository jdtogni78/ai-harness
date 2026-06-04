"""Mine local ``~/.claude/projects/*.jsonl`` rollouts for AskUserQuestion
pairs and produce frozen ``ANSWER`` test cases for the manager-eval corpus
(ai-harness#71, parent #66; unblocks #67's Phase 3 runner by backfilling
``tests/manager_cases/`` instead of waiting on manager-ui's star button).

Pure I/O on top of :mod:`remote_control.eval_cases` -- reuses
``build_case`` / ``action_to_jsonable`` / ``save_case`` so the on-disk
schema stays single-sourced. No network, no analyzer spawn.

Shape it scans for, per rollout file:

* an ``assistant`` turn whose ``message.content`` has a ``tool_use`` block
  with ``name == "AskUserQuestion"``;
* the *next* ``user`` turn carrying a ``tool_result`` keyed by that
  ``tool_use_id``. The top-level ``toolUseResult.answers`` map (when
  present) gives the user's structured picks -- single-select values are
  strings, multi-select are lists; we normalize to comma-joined for the
  case's ``expected.rec_session``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import eval_cases
from .manager import ANSWER, Action, RequiredAction


DEFAULT_SRC = Path.home() / ".claude" / "projects"
DEFAULT_OUT = Path("tests/manager_cases")
# Case kind constant used at the CLI surface; the on-disk Action uses
# `manager.ANSWER` (lowercase "answer") so action_sig() hashes the question.
ANSWER_KIND_LABEL = "ANSWER"


# --------------------------------------------------------------------------- #
# parsing primitives
# --------------------------------------------------------------------------- #
def _iter_jsonl(path: Path) -> Iterator[Tuple[int, dict]]:
    """Yield ``(line_index, parsed_dict)`` for each well-formed object line."""
    try:
        f = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(r, dict):
                yield i, r


def _ask_user_question_tool_uses(rec: dict) -> List[dict]:
    """Every AskUserQuestion ``tool_use`` block on this assistant turn (usually 0 or 1)."""
    if rec.get("type") != "assistant":
        return []
    msg = rec.get("message") or {}
    out: List[dict] = []
    for c in msg.get("content") or []:
        if (isinstance(c, dict) and c.get("type") == "tool_use"
                and c.get("name") == "AskUserQuestion"):
            out.append(c)
    return out


def _answers_for(user_turn: dict) -> Dict[str, Any]:
    """Pull the structured ``answers`` map off the user turn's ``toolUseResult``."""
    tur = user_turn.get("toolUseResult")
    if not isinstance(tur, dict):
        return {}
    answers = tur.get("answers")
    return dict(answers) if isinstance(answers, dict) else {}


def _normalize_answer(v: Any) -> str:
    """Multi-select can come through as ``[label, label]`` OR an already
    comma-joined string; collapse both to ``"a, b"``."""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


# --------------------------------------------------------------------------- #
# repo + session-id derivation
# --------------------------------------------------------------------------- #
def _repo_from_cwd(cwd: Optional[str]) -> Optional[str]:
    """``/Users/<u>/dev/<repo>[/...]`` -> ``<repo>``. Returns None when the
    rollout's cwd isn't under a ``/dev/`` tree we can read."""
    if not cwd or "/dev/" not in cwd:
        return None
    tail = cwd.split("/dev/", 1)[1]
    seg = tail.split("/", 1)[0] if tail else ""
    return seg or None


def _session_id_for(asst_rec: dict, jsonl_path: Path) -> str:
    """Namespace as ``rollout-<uuid8>`` so it can't collide with a live
    ``cse_*`` session id. The rollout's ``sessionId`` field matches the
    jsonl filename's stem; we fall back to the filename if absent."""
    sid = asst_rec.get("sessionId") or jsonl_path.stem
    return "rollout-" + str(sid).replace("-", "")[:8]


# --------------------------------------------------------------------------- #
# transcript tail (matches manager.last_messages() shape)
# --------------------------------------------------------------------------- #
def _transcript_tail(parsed: List[Optional[dict]], stop_index: int,
                     n: int = 6) -> List[Dict[str, str]]:
    """The last *n* user/assistant turns (``[{role, text}]``) before
    ``stop_index`` (exclusive). Drops meta/wrapper/tool-only turns -- the
    same filtering :func:`manager.last_messages` applies to live tails."""
    out: List[Dict[str, str]] = []
    for r in parsed[:stop_index]:
        if not r or r.get("isMeta"):
            continue
        if r.get("type") not in ("user", "assistant"):
            continue
        content = (r.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if not text or text.startswith("<"):
            continue
        out.append({"role": r.get("type"), "text": text})
    return out[-n:]


# --------------------------------------------------------------------------- #
# Action construction
# --------------------------------------------------------------------------- #
def _first_question_text(questions: List[Any]) -> str:
    for q in questions or []:
        if isinstance(q, dict):
            t = (q.get("question") or "").strip()
            if t:
                return t
    return ""


def _build_action(*, asst_rec: dict, tool_use: dict,
                  jsonl_path: Path) -> Action:
    qs_in = (tool_use.get("input") or {}).get("questions")
    questions = list(qs_in) if isinstance(qs_in, list) else []
    req = RequiredAction(
        tool_name="AskUserQuestion",
        tool_use_id=tool_use.get("id") or "",
        description="",  # local rollouts don't carry the server's action_description
        questions=questions,
    )
    cwd = asst_rec.get("cwd") or ""
    return Action(
        session_id=_session_id_for(asst_rec, jsonl_path),
        repo=_repo_from_cwd(cwd),
        kind=ANSWER,
        reason="mined from local rollout",
        run_dir=cwd,
        question=_first_question_text(questions),
        command=[],
        api="",
        managed=True,
        required=req,
        fresh=False,
        title="",
    )


def _expected_rec_session(answers: Dict[str, Any],
                          questions: List[Any]) -> str:
    """The human's picks in question order, verbatim labels, ``", "``-joined.
    Falls back to dict-order values when a question text doesn't map a key
    (the answer-encoder has occasionally truncated keys at quote chars)."""
    parts: List[str] = []
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        qt = (q.get("question") or "").strip()
        if not qt:
            continue
        v = answers.get(qt)
        if v is None or v == "":
            continue
        parts.append(_normalize_answer(v))
    if parts:
        return ", ".join(parts)
    return ", ".join(_normalize_answer(v) for v in answers.values() if v)


# --------------------------------------------------------------------------- #
# the core walk
# --------------------------------------------------------------------------- #
def mine_file(jsonl_path: Path) -> Iterator[Dict[str, Any]]:
    """Yield one ``{action, transcript_tail, expected_rec_session,
    turn_index}`` dict per matched ``(AskUserQuestion, tool_result)`` pair
    in the file. Pairs whose ``tool_result`` is missing, errored, or
    carries no structured ``answers`` are silently skipped (the manager
    only mines what an actual human resolved)."""
    parsed: List[Optional[dict]] = []
    pending: Dict[str, Tuple[int, dict, dict]] = {}
    matched: List[Tuple[int, int, dict, dict]] = []
    # asst_idx, user_idx, asst_rec, tool_use

    for i, rec in _iter_jsonl(jsonl_path):
        # Pad the dense list so transcript-tail can index by line number.
        while len(parsed) < i:
            parsed.append(None)
        parsed.append(rec)

        if rec.get("type") == "user" and pending:
            msg = rec.get("message") or {}
            for c in msg.get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_result":
                    continue
                tuid = c.get("tool_use_id") or ""
                pend = pending.pop(tuid, None)
                if pend:
                    asst_idx, asst_rec, tu = pend
                    matched.append((asst_idx, i, asst_rec, tu))

        for tu in _ask_user_question_tool_uses(rec):
            tuid = tu.get("id") or ""
            if tuid:
                pending[tuid] = (i, rec, tu)

    for asst_idx, user_idx, asst_rec, tool_use in matched:
        user_rec = parsed[user_idx]
        if user_rec is None:
            continue
        answers = _answers_for(user_rec)
        if not answers:
            continue
        action = _build_action(asst_rec=asst_rec, tool_use=tool_use,
                               jsonl_path=jsonl_path)
        questions = (tool_use.get("input") or {}).get("questions") or []
        yield {
            "action": action,
            "transcript_tail": _transcript_tail(parsed, user_idx, n=6),
            "expected_rec_session": _expected_rec_session(answers, questions),
            "turn_index": user_idx,
        }


# --------------------------------------------------------------------------- #
# case assembly + run loop
# --------------------------------------------------------------------------- #
def build_mined_case(*, action: Action,
                     transcript_tail: List[Dict[str, str]],
                     expected_rec_session: str,
                     jsonl_basename: str, turn_index: int,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """Wraps :func:`eval_cases.build_case` with the mined-case defaults and
    re-prefixes the file id with ``mined-`` so the corpus directory keeps
    seed/captured/mined cases visually grouped."""
    case = eval_cases.build_case(
        action=action,
        transcript_tail=transcript_tail,
        actual={"rec_manager": "", "rec_session": "", "analysis": ""},
        expected={"rec_manager": "none", "rec_session": expected_rec_session},
        tags=["A-waiting-q", "ANSWER", "mined"],
        notes=f"mined from {jsonl_basename} turn #{turn_index}",
        captured_by="mined",
        session_meta={},
        now=now,
    )
    case["id"] = "mined-" + case["id"]
    return case


def run_mine(*, src: Path, out: Path, limit: Optional[int] = None,
             dry_run: bool = False, kind: str = "ANSWER",
             stdout=None) -> Dict[str, Any]:
    """Walk every ``<src>/*/*.jsonl``, build cases, upsert into ``out``.
    Returns a summary dict (also printed). ``limit`` caps total
    written+updated cases this run (more pairs may still be *found*)."""
    if stdout is None:
        stdout = sys.stdout
    if kind != ANSWER_KIND_LABEL:
        print(f"unsupported --kind {kind!r} (v1: ANSWER only)", file=sys.stderr)
        return {"ok": False}

    src = Path(src)
    out = Path(out)
    files = sorted(src.glob("*/*.jsonl")) if src.is_dir() else []
    scanned = len(files)
    found = 0
    wrote = 0
    updated = 0
    skipped = 0

    for fp in files:
        for pair in mine_file(fp):
            found += 1
            if limit is not None and (wrote + updated) >= limit:
                skipped += 1
                continue
            try:
                case = build_mined_case(
                    action=pair["action"],
                    transcript_tail=pair["transcript_tail"],
                    expected_rec_session=pair["expected_rec_session"],
                    jsonl_basename=fp.name,
                    turn_index=pair["turn_index"],
                )
            except Exception:
                skipped += 1
                continue
            target = eval_cases.case_path(out, case["id"])
            existed = target.exists()
            if dry_run:
                print(f"[dry-run] would write {target}", file=stdout)
                if existed:
                    updated += 1
                else:
                    wrote += 1
                continue
            eval_cases.save_case(out, case)
            if existed:
                updated += 1
            else:
                wrote += 1

    print(
        f"scanned {scanned} files, found {found} pairs, "
        f"wrote {wrote} new / updated {updated} existing / skipped {skipped}",
        file=stdout,
    )
    return {
        "ok": True,
        "scanned": scanned, "found": found,
        "wrote": wrote, "updated": updated, "skipped": skipped,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m remote_control eval mine",
        description="Backfill manager-eval ANSWER cases from local rollouts.",
    )
    p.add_argument("--src", type=Path, default=DEFAULT_SRC,
                   help="directory of project dirs (default: ~/.claude/projects)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help="case output dir (default: tests/manager_cases)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap total written+updated cases this run")
    p.add_argument("--kind", default="ANSWER",
                   help="case kind to mine (v1: ANSWER only)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be written; touch no files")
    args = p.parse_args(argv)
    res = run_mine(src=args.src, out=args.out, limit=args.limit,
                   dry_run=args.dry_run, kind=args.kind)
    return 0 if res.get("ok") else 2
