"""Test-case I/O for the manager guideline eval harness (ai-harness#37).

A case is a self-contained ``(input, expected)`` snapshot of one situation the
manager analyzed, written by manager-ui's *Save as test case* button and read
by the offline runner (``python -m remote_control eval ...``, phase 3).

The snapshot freezes the live ``Action`` + the transcript tail at capture
time, so cases stay reproducible after the source session is archived or its
on-disk transcript rotates. Shape lives in
``docs/manager-eval-harness.md`` and in ``tests/manager_cases/README.md``;
``CASE_SCHEMA_VERSION`` lets a future schema change be detected without
silently misreading old files.

Pure I/O -- no live network calls and no analyzer spawn. ``manager_ui``
imports :func:`save_case`; phase 3's runner imports :func:`load_case` /
:func:`list_cases` / :func:`action_from_jsonable`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import manager
from .manager import Action, RequiredAction


# Bumped on incompatible schema changes (e.g. renamed/removed fields). Cases
# carrying an unknown version are loaded with a warning by the eval runner.
CASE_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# (de)serialization for Action / RequiredAction (both are NamedTuples, so the
# default json encoder turns them into anonymous arrays -- we want dicts)
# --------------------------------------------------------------------------- #
def action_to_jsonable(a: Action) -> Dict[str, Any]:
    """Action -> plain ``dict`` (with the nested RequiredAction also unpacked).
    Round-trips through :func:`action_from_jsonable`."""
    d = dict(a._asdict())
    req = d.get("required")
    if req is not None:
        d["required"] = dict(req._asdict())
    return d


def action_from_jsonable(d: Dict[str, Any]) -> Action:
    """Inverse of :func:`action_to_jsonable`. The runner uses this to rebuild
    the Action that gets handed to :func:`manager.analyze_action`."""
    d = dict(d)
    req = d.get("required")
    if req is not None:
        d["required"] = RequiredAction(**req)
    return Action(**d)


# --------------------------------------------------------------------------- #
# case id + path -- one case per (kind, session, question)
# --------------------------------------------------------------------------- #
_SIG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def case_id_from_sig(sig: str) -> str:
    """File-safe id derived from :func:`manager.action_sig` (``kind:session:qhash``).
    The trailing ``:`` for non-ANSWER cases (empty qhash) is collapsed."""
    slug = _SIG_UNSAFE.sub("-", sig).strip("-")
    return slug or "case"


def case_path(test_cases_dir: Path, case_id: str) -> Path:
    return test_cases_dir / f"{case_id}.json"


# --------------------------------------------------------------------------- #
# save / load / list
# --------------------------------------------------------------------------- #
def build_case(
    *,
    action: Action,
    transcript_tail: List[Dict[str, str]],
    actual: Dict[str, str],
    expected: Dict[str, str],
    tags: Optional[List[str]] = None,
    notes: str = "",
    captured_by: str = "manager-ui",
    session_meta: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble the case dict that ``save_case`` writes. Pure -- takes the
    already-gathered pieces and bundles them with metadata + the schema
    version. Split out from :func:`save_case` so tests can build a case
    without touching disk."""
    now = now or datetime.now(timezone.utc)
    sig = manager.action_sig(action)
    return {
        "schema": CASE_SCHEMA_VERSION,
        "id": case_id_from_sig(sig),
        "sig": sig,
        "captured_at": now.isoformat(timespec="seconds"),
        "captured_by": captured_by,
        "tags": list(tags or []),
        "notes": notes,
        "input": {
            "action": action_to_jsonable(action),
            "transcript_tail": list(transcript_tail or []),
            "session_meta": dict(session_meta or {}),
        },
        "actual_at_capture": {
            "rec_manager": (actual.get("rec_manager") or "").strip(),
            "rec_session": (actual.get("rec_session") or "").strip(),
            "analysis": (actual.get("analysis") or "").strip(),
        },
        "expected": {
            "rec_manager": (expected.get("rec_manager") or "").strip(),
            "rec_session": (expected.get("rec_session") or "").strip(),
        },
    }


def save_case(test_cases_dir: Path, case: Dict[str, Any]) -> Path:
    """Write a case dict to ``<test_cases_dir>/<id>.json`` atomically (write
    to ``.tmp`` then rename). Overwrites an existing file with the same id --
    captures are upserts so the same situation can be re-saved as expectations
    are refined. Returns the final path."""
    cid = case.get("id") or "case"
    path = case_path(test_cases_dir, cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(case, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)
    return path


def load_case(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def list_cases(test_cases_dir: Path) -> List[Dict[str, Any]]:
    """All cases in the dir (sorted by id, deterministic for the runner).
    Returns ``[]`` when the dir doesn't exist yet (a fresh checkout)."""
    p = Path(test_cases_dir)
    if not p.is_dir():
        return []
    return [load_case(f) for f in sorted(p.glob("*.json"))]


def iter_case_files(test_cases_dir: Path) -> Iterable[Path]:
    p = Path(test_cases_dir)
    if not p.is_dir():
        return iter(())
    return iter(sorted(p.glob("*.json")))
