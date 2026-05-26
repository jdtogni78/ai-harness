"""``python3 -m remote_control manager`` -- the autonomous session shepherd.

Reads every Claude Code session's state (via the code-sessions API the
usage-limit monitor already speaks), classifies each into one actionable state,
and -- for sessions whose repo is in the allowlist -- plans the matching action:

  * **waiting on a question** (``worker_status == requires_action``) -> read the
    structured question from the API (``requires_action_details``; usually an
    ``AskUserQuestion`` tool call with multiple-choice options), spawn a headless
    ``claude -p`` *investigator* in the repo's MAIN checkout to pick the best
    option, then submit that choice as a tool_result. (The investigator runs in
    the main checkout, not the paused session's bridge worktree, so it never
    disturbs or pollutes it.)
  * **idle, no question, looks done** (idle past the done-grace, connected, not
    limit-paused) -> investigator reviews the work and either recommends
    ``/close-work`` (looks done) or proposes the next step.
  * **broken / stale** (``connection_status == disconnected`` past the
    rescue-grace) -> ``fork`` the session, resume work on the fork, archive the
    stale original.
  * **usage-limit paused** -> *defer*: the usage-limit monitor already owns it.
  * **running, or too-recent** -> *skip*: a quiet "running" worker is
    indistinguishable from a long tool call, and a fresh question deserves a
    human first (the grace windows enforce that).

This is intentionally **dry-run by default** (``MANAGER_DRY_RUN=1``): answering a
question, nudging a session, and archiving are all outward-facing and hard to
undo. Dry-run prints the exact investigator command / API call / fork plan it
*would* run and touches nothing. ``--go`` (or ``MANAGER_DRY_RUN=0``) takes the
actions live -- but only the **answer** path is wired so far: REVIEW and RESCUE
still print their plan and are skipped live (RESCUE's archive verb and the
cloud-session pending-question read are still to be confirmed against the API).
The investigator runs read-only (``--permission-mode plan``) because it shares
the paused session's bridge worktree and must not edit it.

The pure helpers (classification, cooldown, prompt + request building, transcript
parsing, rendering) live above ``main`` so they unit-test against plain values;
the I/O (token, list, transcript read, state, spawn/POST) lives in ``main`` and
its injectable seams. Detection deliberately reuses :mod:`inventory` /
:mod:`session_list` / :mod:`usage_limit.detect` rather than re-deriving state.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from .config import DEV, ManagerConfig, UsageLimitConfig
from .discovery import host_allows, load_allowlist
from .inventory import parse_ts
from .session_list import build_rows
from .session_titles import build_worktree_index, repo_for_session
from .usage_limit import detect, monitor

# Action kinds (also the verbs printed in the plan).
DEFER = "defer"      # usage-limit paused -> usage-limit monitor owns it
ANSWER = "answer"    # waiting on a question -> investigate + reply
REVIEW = "review"    # idle, no question -> judge done? close-work / what's next
RESCUE = "rescue"    # broken/stale -> fork + resume + archive
SKIP = "skip"        # nothing to do (active, running, too-recent, on cooldown)


# --------------------------------------------------------------------------- #
# Pure: classification
# --------------------------------------------------------------------------- #
def idle_seconds(last_event_at: str, now: datetime) -> Optional[float]:
    """Seconds since the session's last event, or None if the timestamp is
    missing/unparseable (we can't reason about an unknown age -> caller skips).
    A future timestamp clamps to 0 (treat as just-now)."""
    dt = parse_ts(last_event_at)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())


def classify(
    *,
    worker_status: str,
    connection_status: str,
    limit_paused: bool,
    idle_secs: Optional[float],
    cfg: ManagerConfig,
) -> Tuple[str, str]:
    """Map a session's signals to an (action_kind, reason).

    A thread is **actionable** whenever there is something the manager could do:
    a pending question (ANSWER), an idle session to review (REVIEW), or a dead
    connection to rescue (RESCUE) -- *regardless of how recent* it is. Recency
    ("freshness", within a grace window) is reported separately by :func:`is_fresh`
    and only governs whether the *auto-loop* jumps in ahead of a human; it no
    longer hides a thread from the actionable list. Only a busy ``running`` worker,
    an unknown state, or a usage-limit pause are non-actionable (the last is owned
    by the usage-monitor). Priority: usage-limit, then a question, then a dead
    connection, then idle."""
    if limit_paused:
        return SKIP, "usage-limit paused (usage-monitor owns it)"  # == DEFER, rendered as skip
    if idle_secs is None:
        return SKIP, "unknown last-event time"
    if worker_status == "requires_action":
        return ANSWER, f"waiting on a question for {int(idle_secs)}s"
    if connection_status == "disconnected":
        return RESCUE, f"disconnected (server gone) for {int(idle_secs)}s"
    if worker_status == "running":
        # "running" is trusted only while events are still flowing; a long silence
        # means it's likely hung despite reporting progress -> surface it.
        if idle_secs >= cfg.stuck_running_secs:
            return REVIEW, (f"'running' but no events for {int(idle_secs)}s "
                            "-- likely stale/hung, not progressing")
        return SKIP, "worker running (long tool call vs hung is ambiguous)"
    if worker_status == "idle":
        return REVIEW, f"idle with no pending question for {int(idle_secs)}s"
    return SKIP, f"unhandled worker_status={worker_status!r}"


def is_fresh(kind: str, idle_secs: Optional[float], cfg: ManagerConfig) -> bool:
    """True if an actionable thread is still within its grace window (a human
    deserves first crack, or a blip may settle). The *auto*-loop waits for this to
    clear before acting; the ui surfaces it as actionable either way, just flagged
    'within grace' so a manual action is a deliberate choice."""
    if idle_secs is None:
        return False
    grace = {ANSWER: cfg.answer_grace_secs, REVIEW: cfg.done_grace_secs,
             RESCUE: cfg.rescue_grace_secs}.get(kind)
    return grace is not None and idle_secs < grace


def defer_for_limit(limit_paused: bool) -> str:
    """Render label: a limit-paused session is reported as DEFER (not SKIP) so
    the plan reads truthfully even though classify() returns SKIP for it."""
    return DEFER if limit_paused else SKIP


def cooldown_ok(state_entry: Optional[dict], now: float, cooldown_secs: int) -> bool:
    """True if enough time has elapsed since the last manager action on this
    session (no entry = never acted -> ok)."""
    if not state_entry:
        return True
    last = state_entry.get("last_action_at")
    if last is None:
        return True
    return (now - last) >= cooldown_secs


# --------------------------------------------------------------------------- #
# Pure: transcript reading (best-effort, for the pending question / last turn)
# --------------------------------------------------------------------------- #
def last_assistant_text(lines) -> Optional[str]:
    """The text of the newest ``assistant`` turn in a Claude transcript -- the
    message a ``requires_action`` session is paused on (its question / prompt).

    Defensive: tolerates non-JSON lines and the two content shapes (a plain
    string, or a list of ``{type: text, text}`` blocks). Returns None if no
    assistant text is found."""
    found: Optional[str] = None
    for line in lines:
        s = line.rstrip("\n")
        if not s.strip():
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
        text = text.strip()
        if text:
            found = text  # keep the newest non-empty assistant turn
    return found


# --------------------------------------------------------------------------- #
# Pure: action plans (the exact commands / requests an action would run)
# --------------------------------------------------------------------------- #
def investigator_prompt(kind: str, *, repo: Optional[str], run_dir: str,
                        question: Optional[str]) -> str:
    """The prompt handed to the headless investigator for an ANSWER/REVIEW."""
    where = f"repo {repo!r}" if repo else "this repository"
    if kind == ANSWER:
        q = question or "(could not read the pending question from the transcript)"
        return (
            f"You are a manager agent assisting an autonomous coding session in {where} "
            f"(working dir {run_dir}). The session has PAUSED to ask a question and is "
            f"waiting on a reply. Its pending question is:\n\n---\n{q}\n---\n\n"
            "Investigate the repository as needed and decide the best concrete answer "
            "so the session can continue. Reply with ONLY the answer text to send back "
            "(no preamble, no markdown headers)."
        )
    # REVIEW
    return (
        f"You are a manager agent reviewing an autonomous coding session in {where} "
        f"(working dir {run_dir}) that has gone idle with no pending question. Review the "
        "recent work and decide whether the task looks DONE or has an obvious NEXT step.\n"
        "Reply with EXACTLY one line:\n"
        "  DONE: <one-line summary>     (if it looks finished -- the manager will "
        "recommend running /close-work)\n"
        "  NEXT: <the next instruction> (if there is clearly more to do)"
    )


def investigator_command(claude_bin, prompt: str, model: Optional[str],
                         permission_mode: str = "plan") -> List[str]:
    """Headless ``claude -p`` argv for the investigator.

    Defaults to ``--permission-mode plan`` so it explores read-only and never
    edits the paused session's shared bridge worktree (an empty *permission_mode*
    omits the flag)."""
    cmd = [str(claude_bin), "-p"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


def read_guidelines(cfg: ManagerConfig) -> str:
    """The guideline doc handed to every analysis run as policy (empty if absent)."""
    try:
        return cfg.guidelines_file.read_text()
    except OSError:
        return ""


def read_feedback(cfg: ManagerConfig) -> dict:
    """The operator-feedback map (sig -> {feedback, at}), or {} (written by the ui)."""
    try:
        data = json.loads(cfg.feedback_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_feedback_entry(entry) -> List[dict]:
    """A feedback-store value -> a list of ``{text, at}`` notes. Handles the legacy
    single-note shape (``{feedback, at}``) and the current list-of-notes shape."""
    if isinstance(entry, list):
        return [{"text": (i.get("text") or "").strip(), "at": i.get("at", "")}
                for i in entry if isinstance(i, dict) and (i.get("text") or "").strip()]
    if isinstance(entry, dict):
        t = (entry.get("feedback") or entry.get("text") or "").strip()
        return [{"text": t, "at": entry.get("at", "")}] if t else []
    return []


def feedback_for_prompt(cfg: ManagerConfig, a: Action, *, max_other: int = 3) -> str:
    """Operator feedback relevant to analyzing this thread: ALL of its OWN notes (the
    strongest corrections) plus the latest note from other threads of the same case.
    Rendered for the investigator prompt; '' if there's none."""
    fb = read_feedback(cfg)
    if not fb:
        return ""
    lines = [f"- (this exact thread) {e['text']}"
             for e in normalize_feedback_entry(fb.get(action_sig(a)))]
    others = []
    for sig, v in fb.items():
        if sig == action_sig(a) or not sig.startswith(a.kind + ":"):
            continue
        notes = normalize_feedback_entry(v)
        if notes:
            others.append((notes[-1].get("at", ""), notes[-1]["text"]))
    others.sort(reverse=True)
    for _, t in others[:max_other]:
        lines.append(f"- (another {a.kind}) {t}")
    return "\n".join(lines)


def investigator_prompt_advise(a: Action, *, guidelines: str = "",
                               feedback: str = "") -> str:
    """Prompt for the investigator to REVIEW one stuck thread and report what the
    operator should do: a one-line recommendation + ~5 sentences of analysis. The
    current guidelines and any prior operator feedback are injected as policy."""
    where = f"repo {a.repo!r}" if a.repo else "this repository"
    if a.kind == ANSWER:
        ctx = ("It has PAUSED on a question and is waiting on a reply:\n"
               f"---\n{a.question or '(question unavailable)'}\n---\n"
               "Decide which option / answer is best.")
    elif a.kind == REVIEW:
        ctx = ("It has no pending question and has stopped making visible progress "
               "(idle, or 'running' but silent for a long time -- possibly hung). "
               "Judge whether the work looks DONE (operator should run /close-work), "
               "is genuinely still working (leave it), or is stuck/hung and needs a "
               "nudge or a rescue (fork + resume).")
    elif a.kind == RESCUE:
        ctx = ("Its connection is GONE (the agent likely disconnected/crashed). "
               "Judge whether to fork + resume + archive it, or whether its work "
               "already landed so it should just be archived.")
    else:  # SKIP / DEFER -- not obviously actionable, but reviewable on request
        ctx = (f"It is not in an obviously-actionable state (current read: {a.reason}). "
               "Investigate whether it is actually fine (just working) or quietly "
               "stuck/needing attention, and report whether anything is needed.")
    g = (f"\nCURRENT GUIDELINES (the policy to follow -- weigh these heavily):\n"
         f"---\n{guidelines.strip()}\n---\n" if guidelines and guidelines.strip() else "")
    f = (f"\nPRIOR OPERATOR FEEDBACK on past recommendations -- treat as "
         f"authoritative corrections and follow them:\n{feedback.strip()}\n"
         if feedback and feedback.strip() else "")
    return (
        f"You are a manager agent reviewing an autonomous coding session "
        f"({a.session_id}) in {where} (code at {a.run_dir or '?'}). "
        f"It looks STUCK: {a.reason}.\n{ctx}\n{g}{f}\n"
        "Investigate the repository/situation as needed (read-only), then report "
        "what should happen, split by WHO acts. Reply EXACTLY in this format, "
        "nothing else:\n"
        "ANALYSIS: <about 5 sentences: what's going on, the key evidence, the plan, "
        "and any risk>\n"
        "MANAGER: <the lifecycle op the MANAGER does itself because the session "
        "CANNOT do it to itself: ARCHIVE the session once its work is confirmed "
        "finished (e.g. after /close-work has landed), or fork + resume a broken "
        "one. Do NOT put /close-work here -- that is a SESSION step. Only archive "
        "when the work is truly done and ready; if unsure, write exactly \"none\". "
        "Write exactly \"none\" (nothing else) when there is nothing.>\n"
        "SESSION: <the guidance to deliver INTO the live session for IT to do next "
        "while it is alive: answer its pending question, run /close-work to wrap up "
        "+ deliver a finished thread, the next instruction, \"write down notes\", "
        "\"file a follow-up ticket\", \"separate the prod work\". Write exactly "
        "\"none\" (nothing else) when there is nothing.>"
    )


# The titled sections an advice reply carries (order-independent; multi-line).
_SECTION_RE = re.compile(r"(?i)^\s*(manager|session|analysis|recommendation)\s*:\s*(.*)$")
_NONE_VALUES = {"", "none", "none.", "n/a", "na", "-", "(none)", "nothing", "nothing."}
# Also treat a justified "none" as empty -- e.g. "none -- already delivered",
# "none." -- but NOT "none of the tests pass" (a space+word is real content).
_NONE_RE = re.compile(r"(?i)^\s*none\s*[—\-.,:;)]")


def _is_none(text: str) -> bool:
    """A section the investigator left empty (its 'nothing to do here' marker),
    tolerating a 'none' the model tacked a justification onto."""
    return (text or "").strip().lower() in _NONE_VALUES or bool(_NONE_RE.match(text or ""))


def format_rec(rec_manager: str, rec_session: str) -> str:
    """The combined, titled recommendation shown in the ui / logs, rebuilt from the
    parsed MANAGER / SESSION halves (so a stored record carries one readable line)."""
    parts = []
    if rec_manager.strip():
        parts.append(f"MANAGER: {rec_manager.strip()}")
    if rec_session.strip():
        parts.append(f"SESSION: {rec_session.strip()}")
    return "\n".join(parts)


def parse_advice(text: str) -> Tuple[str, str, str]:
    """Split investigator output into ``(manager_rec, session_rec, analysis)`` by its
    titled sections. A ``none`` section becomes "". Tolerant: a legacy
    ``RECOMMENDATION:`` maps to the SESSION half, and wholly-unlabelled prose
    becomes a SESSION rec (never a MANAGER one -- so malformed output is never
    auto-executed) plus the analysis, so nothing is lost."""
    text = (text or "").strip()
    if not text:
        return "", "", ""
    sections: Dict[str, str] = {}
    current = None
    buf: List[str] = []
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current, buf = m.group(1).lower(), [m.group(2)]
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    if not sections:                                  # unlabelled prose
        return "", text, text
    manager = sections.get("manager", "")
    session = sections.get("session", "")
    analysis = sections.get("analysis", "")
    if "recommendation" in sections and not (manager or session):
        session = sections["recommendation"]          # legacy single-rec format
        analysis = analysis or sections["recommendation"]
    manager = "" if _is_none(manager) else manager.strip()
    session = "" if _is_none(session) else session.strip()
    return manager, session, analysis.strip()


def action_sig(a: Action) -> str:
    """Cache key for an analysis: same situation -> same sig (so the UI / loop
    won't re-spawn `claude -p` for it). The question hash makes a *new* question a
    new situation; REVIEW/RESCUE key on session + case."""
    qh = _question_hash(a.question) if a.kind == ANSWER else ""
    return f"{a.kind}:{a.session_id}:{qh}"


def analyze_action(a: Action, *, cfg: ManagerConfig, guidelines: str = "",
                   log, runner=subprocess.run) -> dict:
    """Run the investigator (`claude -p`) on one stuck thread WITH the guidelines
    and parse its recommendation + ~5-sentence analysis. Read-only; never submits.
    Returns ``{ok, recommendation, analysis, note, raw}``."""
    # Fall back to the dev root when the session can't be mapped to a repo checkout
    # (cloud / other-host / unrecognized source) so the investigator still runs and
    # can reason from the session metadata, rather than silently producing nothing.
    run_dir = a.run_dir or (str(cfg.dev / a.repo) if a.repo else str(cfg.dev))
    prompt = investigator_prompt_advise(a, guidelines=guidelines,
                                        feedback=feedback_for_prompt(cfg, a))
    cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                               cfg.investigator_permission_mode)
    out = run_investigator(cmd, run_dir, cfg.investigator_timeout_secs,
                           runner=runner, log=log)
    if not out:
        return {"ok": False, "recommendation": "", "rec_manager": "",
                "rec_session": "", "analysis": "",
                "note": "investigator produced no output", "raw": ""}
    rec_manager, rec_session, ana = parse_advice(out)
    return {"ok": True, "rec_manager": rec_manager, "rec_session": rec_session,
            "recommendation": format_rec(rec_manager, rec_session),
            "analysis": ana, "note": "analyzed", "raw": out}


def executor_prompt(a: Action, manager_rec: str, *, guidelines: str = "") -> str:
    """Prompt for the manager-EXECUTOR: a headless ``claude -p`` that can ACT (not
    the read-only ``plan`` investigator) and carries out the MANAGER half of a
    recommendation -- lifecycle ops the session can't do to itself, like archiving
    a finished session or fork + resume of a broken one. (NB: /close-work is a
    SESSION action -- the session runs it itself -- not a MANAGER one.)"""
    where = f"repo {a.repo!r}" if a.repo else "this repository"
    g = (f"\nGUIDELINES (policy -- follow these):\n---\n{guidelines.strip()}\n---\n"
         if guidelines and guidelines.strip() else "")
    return (
        f"You are the session-manager's EXECUTOR, acting for the operator on the "
        f"autonomous coding session {a.session_id} in {where} (code at "
        f"{a.run_dir or '?'}). Carry out the manager action below now -- a lifecycle "
        f"op the session can't do to itself (e.g. archive a finished session, or "
        f"fork + resume a broken one). Do exactly what is asked and nothing more. If "
        f"it looks unsafe, destructive, or ambiguous, STOP and report why instead of "
        f"guessing.{g}\n"
        f"MANAGER ACTION:\n{manager_rec.strip()}\n"
    )


def execute_manager_rec(a: Action, *, cfg: ManagerConfig, manager_rec: str,
                        guidelines: str = "", log,
                        runner=subprocess.run) -> dict:
    """Spawn a headless ``claude -p`` (allowed to act) to carry out the MANAGER half
    of a recommendation for one thread. Gated by ``cfg.execute_enabled``: when off
    (default) it logs the would-be command and does NOT spawn (shadow mode),
    mirroring the answer path's ``submit_enabled``. Returns
    ``{ok, ran, note, output}`` (``ran`` is True only when a process was spawned)."""
    if not manager_rec or not manager_rec.strip():
        return {"ok": False, "ran": False, "note": "no manager action", "output": ""}
    run_dir = a.run_dir or (str(cfg.dev / a.repo) if a.repo else str(cfg.dev))
    prompt = executor_prompt(a, manager_rec, guidelines=guidelines)
    cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                               cfg.executor_permission_mode)
    if not cfg.execute_enabled:
        log(f"execute {a.session_id}: MANAGER_EXECUTE_ENABLED=0 (shadow) -- not "
            f"spawning; would run in {run_dir}: {manager_rec.strip()[:120]!r}")
        return {"ok": True, "ran": False, "note": "execute disabled (shadow)",
                "output": ""}
    out = run_investigator(cmd, run_dir, cfg.executor_timeout_secs,
                           runner=runner, log=log)
    if out is None:
        return {"ok": False, "ran": True,
                "note": "executor failed / produced no output", "output": ""}
    log(f"executed {a.session_id} MANAGER rec -> {out[:160]}")
    return {"ok": True, "ran": True, "note": "executed", "output": out}


def guidelines_writer_prompt(current: str, feedback_items: List[Tuple[str, str]]) -> str:
    """Prompt the guideline-writer to fold operator feedback into the doc."""
    fb = "\n".join(f"- [{kind}] {txt}" for kind, txt in feedback_items) or "(none)"
    return (
        "You maintain the guideline document that an autonomous session-manager "
        "consults when deciding what to do with stuck coding sessions (cases: answer "
        "a waiting question, review an idle one, rescue a broken one). Below is the "
        "CURRENT document, then OPERATOR FEEDBACK gathered from reviewing the "
        "manager's recommendations. Revise the document so it durably captures the "
        "lessons in the feedback: refine or add concise `GUIDELINE:` lines under the "
        "right case, keep the existing structure and anything still valid, and don't "
        "invent cases with no basis in the feedback. Output ONLY the full revised "
        "markdown document -- no preamble, no fences.\n\n"
        f"=== CURRENT GUIDELINES ===\n{current or '(empty)'}\n\n"
        f"=== OPERATOR FEEDBACK ===\n{fb}\n"
    )


def suggest_guidelines(cfg: ManagerConfig, *, log, runner=subprocess.run) -> dict:
    """Run a `claude -p` writer that revises the guideline doc from the accumulated
    feedback corpus. Returns ``{ok, text}`` (the PROPOSED doc -- never written here;
    the ui shows it for the operator to review + Save)."""
    fb = read_feedback(cfg)
    rows = []
    for sig, v in fb.items():
        for note in normalize_feedback_entry(v):
            rows.append((note.get("at", ""), sig.split(":", 1)[0], note["text"]))
    rows.sort(reverse=True)   # newest first
    items = [(kind, text) for _, kind, text in rows]
    if not items:
        return {"ok": False, "error": "no feedback yet to learn from"}
    prompt = guidelines_writer_prompt(read_guidelines(cfg), items)
    cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                               cfg.investigator_permission_mode)
    repo_root = cfg.guidelines_file.parent.parent
    run_dir = str(repo_root) if repo_root.is_dir() else str(cfg.dev)
    out = run_investigator(cmd, run_dir, cfg.investigator_timeout_secs,
                           runner=runner, log=log)
    if not out:
        return {"ok": False, "error": "writer produced no output"}
    return {"ok": True, "text": out}


def answer_request(session_id: str, text: str) -> Tuple[str, str, dict]:
    """(method, path, body) to deliver *text* into a session as a user turn --
    the same /events shape the usage-monitor resumes with."""
    return "POST", f"/sessions/{session_id}/events", detect.resume_event_body(text)


def archive_request(session_id: str) -> Tuple[str, str, dict]:
    """(method, path, body) to archive a session via the verified live verb
    ``POST /sessions/{id}/archive`` (probed 2026-05; ``PUT`` with
    ``status=archived`` returned 405). Mirrors ``monitor.archive_session``."""
    return "POST", f"/sessions/{session_id}/archive", {}


# --------------------------------------------------------------------------- #
# Pure: the structured "what is it waiting on" (AskUserQuestion tool calls)
# --------------------------------------------------------------------------- #
# A session that "waits on a question" is, in practice, blocked on a tool call
# (usually AskUserQuestion) carrying STRUCTURED multiple-choice questions -- the
# API exposes this authoritatively in `requires_action_details`, so we read it
# there rather than scraping the transcript. Answering means submitting the
# chosen option label(s) for that tool_use_id, not free text.
class RequiredAction(NamedTuple):
    tool_name: str            # e.g. "AskUserQuestion"
    tool_use_id: str
    description: str          # action_description
    questions: List[dict]     # [{question, header, options:[{label,description}], multiSelect}]


def parse_required_action(detail: Optional[dict]) -> Optional[RequiredAction]:
    """Parse a session detail's ``requires_action_details`` (already unwrapped
    from ``response_shape``) into a :class:`RequiredAction`, or None if absent."""
    rad = (detail or {}).get("requires_action_details")
    if not isinstance(rad, dict):
        return None
    questions = (rad.get("input") or {}).get("questions")
    return RequiredAction(
        tool_name=rad.get("tool_name") or "",
        tool_use_id=rad.get("tool_use_id") or "",
        description=rad.get("action_description") or "",
        questions=questions if isinstance(questions, list) else [],
    )


def option_labels(question: dict) -> List[str]:
    return [o.get("label", "") for o in (question.get("options") or [])
            if isinstance(o, dict)]


def format_questions(ra: RequiredAction) -> str:
    """A readable rendering of the structured questions + options (for both the
    investigator prompt and the watch/report dashboard)."""
    out: List[str] = []
    for i, q in enumerate(ra.questions, 1):
        ms = "  (multi-select)" if q.get("multiSelect") else ""
        out.append(f"Q{i} [{q.get('header', '')}]: {q.get('question', '')}{ms}")
        for o in (q.get("options") or []):
            if isinstance(o, dict):
                out.append(f"    - {o.get('label', '')}: {o.get('description', '')}")
    return "\n".join(out)


_CHOICE_RE = re.compile(r"^\s*Q(\d+)\s*[:.)]\s*(.+?)\s*$")


def parse_choices(text: str, ra: RequiredAction) -> Optional[List[dict]]:
    """Map the investigator's ``Q<n>: <label>`` lines to one validated selection
    per question (in order). Returns None if any question is unanswered or a
    chosen label doesn't match that question's options (so an unparseable or
    hallucinated answer never gets submitted)."""
    chosen: Dict[int, str] = {}
    for line in (text or "").splitlines():
        m = _CHOICE_RE.match(line)
        if m:
            chosen[int(m.group(1))] = m.group(2).strip()
    selections: List[dict] = []
    for i, q in enumerate(ra.questions, 1):
        want = chosen.get(i)
        if not want:
            return None
        labels = option_labels(q)
        match = (next((L for L in labels if L == want), None)
                 or next((L for L in labels if L.lower() == want.lower()), None))
        if match is None:
            return None
        selections.append({"header": q.get("header", ""),
                           "question": q.get("question", ""), "label": match})
    return selections


def investigator_prompt_answer(ra: RequiredAction, *, repo: Optional[str],
                               run_dir: str) -> str:
    """Prompt for the investigator to CHOOSE among an AskUserQuestion's options."""
    where = f"repo {repo!r}" if repo else "this repository"
    return (
        f"You are a manager agent helping an autonomous coding session in {where} "
        f"(code at {run_dir}). It has PAUSED on a multiple-choice question and is "
        "waiting for a decision. Investigate the repository as needed, then choose the "
        "single best option for EACH question below.\n\n"
        f"{format_questions(ra)}\n\n"
        "Reply with EXACTLY one line per question and nothing else, each formatted as:\n"
        "Q<number>: <the exact option label you choose>"
    )


def selection_event_body(ra: RequiredAction, selections: List[dict]) -> dict:
    """Best-guess ``/events`` body to answer an AskUserQuestion tool call with the
    chosen option label(s), as a tool_result keyed to its ``tool_use_id``.

    SHAPE UNVERIFIED -- a free-text user event does NOT resolve the tool call
    (confirmed live: the session stays ``requires_action``); this tool_result
    shape is the candidate to confirm via a careful live probe before it's used."""
    answer = "\n".join(f"{s['header']}: {s['label']}" for s in selections)
    return {"events": [{
        "event_type": "user",
        "source": "client",
        "payload": {
            "type": "user",
            "message": {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": ra.tool_use_id,
                "content": [{"type": "text", "text": answer}],
            }]},
        },
    }]}


class Action(NamedTuple):
    session_id: str
    repo: Optional[str]
    kind: str            # DEFER | ANSWER | REVIEW | RESCUE | SKIP
    reason: str
    run_dir: str = ""    # cwd an investigator / fork would run in
    question: str = ""   # rendered pending question(s) for display (ANSWER)
    command: List[str] = []   # investigator argv (ANSWER/REVIEW)
    api: str = ""        # one-line description of the API call(s) it would make
    managed: bool = True # False = repo not in allowlist (reported, never acted)
    required: Optional[RequiredAction] = None  # structured AskUserQuestion (ANSWER)
    fresh: bool = False  # actionable but still within its grace window (auto-loop waits)
    title: str = ""      # the session's title (for the ui)


def format_plan(actions: List[Action], host: str, dry_run: bool) -> str:
    """A scannable per-session plan block."""
    if not actions:
        return "(no sessions)"
    verb = "WOULD" if dry_run else "WILL"
    out: List[str] = []
    for a in actions:
        tag = a.kind.upper() if a.managed else "UNMANAGED"
        out.append(f"[{tag:<10}] {a.session_id}  ({a.repo or '<unknown repo>'})")
        out.append(f"    why  : {a.reason}")
        if a.kind == ANSWER and a.question:
            for ln in a.question.splitlines():
                out.append(f"    ask  | {ln}")
        if a.kind in (ANSWER, REVIEW):
            out.append(f"    {verb} run investigator in {a.run_dir or '<unknown>'}:")
            out.append(f"      {' '.join(repr(c) if ' ' in c else c for c in a.command)}")
            out.append(f"    then : {a.api}")
        elif a.kind == RESCUE:
            out.append(f"    {verb}: {a.api}")
        out.append("")
    return "\n".join(out).rstrip()


def summarize(actions: List[Action]) -> str:
    counts: Dict[str, int] = {}
    for a in actions:
        key = a.kind if a.managed else "unmanaged"
        counts[key] = counts.get(key, 0) + 1
    return "by action: " + ", ".join(
        f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def read_state(cfg: ManagerConfig) -> dict:
    if not cfg.state_file.exists():
        return {"sessions": {}}
    try:
        data = json.loads(cfg.state_file.read_text())
        data.setdefault("sessions", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}


def plan_for_session(
    s: dict,
    *,
    cfg: ManagerConfig,
    repo: Optional[str],
    managed: bool,
    now: datetime,
    now_epoch: float,
    state: dict,
    required: Optional[RequiredAction] = None,
) -> Action:
    """Classify one session and build its concrete (would-be) action plan.

    *required* is the parsed ``requires_action_details`` (the structured pending
    question), fetched by the caller for waiting sessions; the investigator runs
    in the repo's MAIN checkout (``<dev>/<repo>``), never the paused session's
    bridge worktree, so it can't disturb (or pollute the project dir of) the
    session it's helping."""
    sid = s.get("id") or ""
    title = s.get("title") or ""
    limit_paused = detect.limit_pause_detail(s) is not None
    idle_secs = idle_seconds(s.get("last_event_at") or "", now)
    kind, reason = classify(
        worker_status=s.get("worker_status") or "",
        connection_status=s.get("connection_status") or "",
        limit_paused=limit_paused,
        idle_secs=idle_secs,
        cfg=cfg,
    )
    if limit_paused:
        kind = DEFER
    fresh = is_fresh(kind, idle_secs, cfg)
    if fresh:
        reason += " (within grace -- auto-loop waits, but you can act now)"
    if not managed and kind not in (SKIP, DEFER):
        q = format_questions(required) if (kind == ANSWER and required) else ""
        return Action(sid, repo, kind, reason + " (repo not in allowlist)",
                      question=q, managed=False, required=required, fresh=fresh, title=title)
    if kind in (SKIP, DEFER):
        return Action(sid, repo, kind, reason, managed=managed, title=title)
    if not cooldown_ok(state["sessions"].get(sid), now_epoch, cfg.cooldown_secs):
        return Action(sid, repo, SKIP, f"on cooldown (acted < {cfg.cooldown_secs}s ago)",
                      managed=managed, title=title)

    run_dir = str(cfg.dev / repo) if repo else ""
    if kind == ANSWER:
        prompt = (investigator_prompt_answer(required, repo=repo, run_dir=run_dir)
                  if required else
                  investigator_prompt(ANSWER, repo=repo, run_dir=run_dir, question=None))
        cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                                   cfg.investigator_permission_mode)
        api = f"POST /sessions/{sid}/events  (tool_result selecting the chosen option)"
        return Action(sid, repo, ANSWER, reason, run_dir=run_dir,
                      question=format_questions(required) if required else "",
                      command=cmd, api=api, managed=managed, required=required,
                      fresh=fresh, title=title)
    if kind == REVIEW:
        prompt = investigator_prompt(REVIEW, repo=repo, run_dir=run_dir, question=None)
        cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                                   cfg.investigator_permission_mode)
        api = f"POST /sessions/{sid}/events  (DONE->recommend /close-work, else the next step)"
        return Action(sid, repo, REVIEW, reason, run_dir=run_dir,
                      command=cmd, api=api, managed=managed, fresh=fresh, title=title)

    # RESCUE
    api = (f"fork {sid} (--into-main) -> resume the fork "
           f"(claude -r <new> -p {cfg.resume_message!r}) -> archive {sid} "
           f"(PUT /sessions/{sid} status=archived)")
    return Action(sid, repo, RESCUE, reason, run_dir=str(cfg.dev / repo) if repo else "",
                  api=api, managed=managed, fresh=fresh, title=title)


# --------------------------------------------------------------------------- #
# I/O: execution (the answer path)
# --------------------------------------------------------------------------- #
def write_state(cfg: ManagerConfig, state: dict) -> None:
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.state_file)


def _question_hash(question: str) -> str:
    return hashlib.sha256((question or "").encode("utf-8")).hexdigest()[:16]


def record_action(state: dict, sid: str, kind: str, now_epoch: float,
                  *, question: str = "") -> None:
    """Stamp a completed action so the cooldown applies next tick (and we can
    tell, via the question hash, whether a re-ask is genuinely new)."""
    prev = state["sessions"].get(sid, {})
    state["sessions"][sid] = {
        "last_action_kind": kind,
        "last_action_at": now_epoch,
        "attempts": prev.get("attempts", 0) + 1,
        "question_hash": _question_hash(question),
    }


def run_investigator(cmd: List[str], run_dir: Optional[str], timeout: int,
                     *, runner=subprocess.run, log) -> Optional[str]:
    """Run the headless investigator and return its stdout (the answer), or None
    on a missing cwd / spawn failure / timeout / non-zero exit / empty output."""
    if not run_dir or not os.path.isdir(run_dir):
        log(f"investigator skip: run dir missing {run_dir!r}")
        return None
    try:
        proc = runner(cmd, cwd=run_dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"investigator timed out after {timeout}s")
        return None
    except (OSError, ValueError) as e:
        log(f"investigator spawn failed: {e}")
        return None
    if proc.returncode != 0:
        log(f"investigator rc={proc.returncode}: {(proc.stderr or '')[-200:]}")
        return None
    return (proc.stdout or "").strip() or None


def post_answer(ucfg: UsageLimitConfig, token: str, sid: str, text: str,
                *, api=monitor.api_request, log) -> bool:
    """Deliver free-text *text* into a session as a user turn (POST /events).
    Used for REVIEW nudges (prose); NOT for answering a structured tool call."""
    method, path, body = answer_request(sid, text)
    code, resp = api(ucfg, method, path, token, body)
    if code == 200:
        return True
    log(f"answer POST failed {sid} http={code} body={str(resp)[:200]}")
    return False


def get_session_detail(ucfg: UsageLimitConfig, token: str, sid: str,
                       *, api=monitor.api_request, log=lambda m: None) -> Optional[dict]:
    """The session detail (unwrapped from ``response_shape``), or None. Carries
    ``requires_action_details`` -- the structured pending question."""
    code, data = api(ucfg, "GET", f"/sessions/{sid}", token)
    if code != 200 or not isinstance(data, dict):
        log(f"detail GET {sid} http={code}")
        return None
    return data.get("response_shape", data)


def evaluate_answer(a: Action, *, cfg: ManagerConfig, log,
                    runner=subprocess.run) -> Tuple[Optional[List[dict]], str]:
    """Run the investigator on a waiting session and parse its choice. Returns
    (selections, note) -- *selections* is None (with a reason in *note*) when the
    investigator gave no output or its reply can't be mapped to valid option(s),
    so a hallucinated answer never advances. No network, no submission -- this is
    the 'evaluate' half shared by the monitor (report-only) and execute_answer."""
    ra = a.required
    if ra is None or not ra.questions:
        return None, "no structured question to answer"
    out = run_investigator(a.command, a.run_dir, cfg.investigator_timeout_secs,
                           runner=runner, log=log)
    if not out:
        return None, "investigator produced no output"
    selections = parse_choices(out, ra)
    if selections is None:
        return None, f"reply didn't map to valid option(s): {out[:160]!r}"
    return selections, "evaluated"


def execute_answer(a: Action, *, ucfg: UsageLimitConfig, token: str,
                   cfg: ManagerConfig, state: dict, now_epoch: float, log,
                   runner=subprocess.run, api=monitor.api_request) -> bool:
    """Investigate, pick an option, and -- only when submission is enabled --
    submit it as a tool_result. Returns True only when actually submitted."""
    selections, note = evaluate_answer(a, cfg=cfg, log=log, runner=runner)
    if selections is None:
        log(f"answer {a.session_id}: {note}; not submitting")
        return False
    picks = "; ".join(f"{s['header']}={s['label']}" for s in selections)
    log(f"answer {a.session_id}: investigator chose -> {picks}")
    if not cfg.submit_enabled:
        log(f"answer {a.session_id}: submission disabled (MANAGER_SUBMIT_ENABLED=0; "
            f"tool_result shape unverified) -- not posting")
        return False
    method, path, body = ("POST", f"/sessions/{a.session_id}/events",
                          selection_event_body(a.required, selections))
    code, resp = api(ucfg, method, path, token, body)
    if code != 200:
        log(f"answer submit failed {a.session_id} http={code} body={str(resp)[:200]}")
        return False
    record_action(state, a.session_id, ANSWER, now_epoch, question=a.question)
    log(f"answered {a.session_id}: submitted {picks}")
    return True


# --------------------------------------------------------------------------- #
# I/O: monitor / evaluate / report loop (no submission)
# --------------------------------------------------------------------------- #
_running = True


def decision_record(a: Action, *, advice: dict, ts: str) -> dict:
    """One reviewable record of the manager's analysis of a stuck thread: its
    reason, the pending question (if any), and the investigator's recommendation +
    analysis. ``sig`` is the cache key (see :func:`action_sig`)."""
    questions = []
    if a.required:
        questions = [{"header": q.get("header", ""), "question": q.get("question", ""),
                      "options": option_labels(q)} for q in a.required.questions]
    return {
        "ts": ts, "session_id": a.session_id, "repo": a.repo, "case": a.kind,
        "reason": a.reason, "managed": a.managed, "sig": action_sig(a),
        "questions": questions,
        "recommendation": advice.get("recommendation", ""),
        "rec_manager": advice.get("rec_manager", ""),   # the manager executes this
        "rec_session": advice.get("rec_session", ""),   # this is sent to the session
        "analysis": advice.get("analysis", ""),
        "analyzed": bool(advice.get("ok")), "note": advice.get("note", ""),
    }


def append_decision(cfg: ManagerConfig, rec: dict) -> None:
    cfg.decisions_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.decisions_file, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def dump_scenario(cfg: ManagerConfig, a: Action) -> None:
    """Save a waiting session's structured question to the scenario corpus (once
    per distinct question) so it can be ``--replay``ed offline later."""
    ra = a.required
    if not ra or not ra.questions:
        return
    path = cfg.scenarios_dir / f"{a.session_id}-{_question_hash(a.question)}.json"
    if path.exists():
        return
    cfg.scenarios_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": a.session_id, "repo": a.repo, "tool_name": ra.tool_name,
        "tool_use_id": ra.tool_use_id, "questions": ra.questions}, indent=2))


def collect_actions(sessions: List[dict], *, cfg: ManagerConfig,
                    ucfg: UsageLimitConfig, token: str, index: dict, allow: dict,
                    consider_all: bool, want_repo: Optional[str], now: datetime,
                    now_epoch: float, state: dict, log,
                    api=monitor.api_request, dump=False) -> List[Action]:
    """Plan an action for every (non-archived, in-scope) session. Fetches the
    structured question only for waiting sessions; optionally dumps each to the
    scenario corpus."""
    actions: List[Action] = []
    for s in sessions:
        sid = s.get("id")
        if not sid or sid in cfg.skip_session_ids:
            continue
        if (s.get("status") or "") == "archived":
            continue
        repo = repo_for_session(s, index)
        if want_repo is not None and (repo or "").lower() != want_repo:
            continue
        managed = consider_all or (
            repo is not None and repo in allow and host_allows(allow[repo], cfg.host))
        required = None
        if (s.get("worker_status") or "") == "requires_action":
            required = parse_required_action(
                get_session_detail(ucfg, token, sid, api=api, log=log))
        a = plan_for_session(s, cfg=cfg, repo=repo, managed=managed, now=now,
                             now_epoch=now_epoch, state=state, required=required)
        if dump and required and required.questions:
            # carry the structured question for the dump even if the plan SKIPped
            # (cooldown) and dropped it from the Action.
            dump_scenario(cfg, a._replace(required=required,
                                          question=format_questions(required)))
        actions.append(a)
    return actions


def monitor_tick(cfg: ManagerConfig, ucfg: UsageLimitConfig, token: str, *,
                 now: datetime, log, consider_all: bool = False,
                 want_repo: Optional[str] = None, runner=subprocess.run,
                 api=monitor.api_request) -> List[dict]:
    """One analyze(+execute) pass (the backup loop): classify all sessions and, for
    up to max-actions-per-tick stuck ones (answer/review/rescue), run the
    investigator WITH the guidelines to produce a recommendation (a MANAGER half +
    a SESSION half) + analysis, appending a decision record per analysis. The
    MANAGER half is then carried out by a headless ``claude -p`` -- double-gated:
    only when the loop is live (``not dry_run``) AND ``execute_enabled``; otherwise
    it is shadow-logged. The SESSION half is never auto-sent here (the manager-ui
    delivers it on human authorization). Cooldown-gated so it spreads work across
    ticks. Never submits a structured tool answer (that path is separate)."""
    sessions = monitor.list_sessions(ucfg, token, log)
    if sessions is None:
        return []
    index = build_worktree_index(cfg.dev)
    try:
        allow = load_allowlist(cfg.allowlist_file.read_text())
    except OSError:
        allow = {}
    state = read_state(cfg)
    guidelines = read_guidelines(cfg)
    now_epoch = now.timestamp()
    ts = now.isoformat(timespec="seconds")
    actions = collect_actions(sessions, cfg=cfg, ucfg=ucfg, token=token, index=index,
                              allow=allow, consider_all=consider_all, want_repo=want_repo,
                              now=now, now_epoch=now_epoch, state=state, log=log,
                              api=api, dump=True)
    records: List[dict] = []
    analyzed = 0
    for a in actions:
        # Every actionable managed thread is "stuck" and worth analyzing: ANSWER
        # (waiting), REVIEW (idle), RESCUE (broken). SKIP/DEFER and unmanaged carry
        # nothing to review.
        if not a.managed or a.kind not in (ANSWER, REVIEW, RESCUE):
            continue
        # The auto-loop respects the grace window (gives a human first crack); a
        # fresh thread is still shown as actionable in the ui, where a manual
        # Analyze can override. The loop picks it up once it matures.
        if a.fresh:
            continue
        # The cap bounds investigator spawns (cost) per tick; the rest are picked up
        # on later ticks (cooldown keeps the loop from re-spending on the same one).
        if analyzed >= cfg.max_actions_per_tick:
            break
        advice = analyze_action(a, cfg=cfg, guidelines=guidelines, log=log, runner=runner)
        analyzed += 1
        rec = decision_record(a, advice=advice, ts=ts)
        # The MANAGER half is carried out by a `claude -p` executor; the SESSION
        # half is left for the manager-ui to deliver on human authorization. Live
        # execution needs the loop live (not dry-run) AND execute_enabled on --
        # else it's shadow-logged (no spawn), so defaults do nothing.
        if advice.get("rec_manager"):
            if cfg.dry_run:
                rec["executed"] = False
                rec["execute_note"] = "dry-run (MANAGER_DRY_RUN=1) -- not executed"
                log(f"would execute {a.session_id} MANAGER rec (dry-run): "
                    f"{advice['rec_manager'][:120]!r}")
            else:
                ex = execute_manager_rec(a, cfg=cfg, manager_rec=advice["rec_manager"],
                                         guidelines=guidelines, log=log, runner=runner)
                rec["executed"] = ex.get("ran", False)
                rec["execute_note"] = ex.get("note", "")
        records.append(rec)
        append_decision(cfg, rec)
        record_action(state, a.session_id, a.kind, now_epoch, question=a.question)
        log(f"analyzed {a.session_id} [{a.kind}]: "
            f"{advice.get('recommendation') or advice.get('note') or '(none)'}"[:120])
    write_state(cfg, state)
    log(f"tick: {len(actions)} session(s); analyzed {analyzed}; {summarize(actions)}")
    return records


def scan_actions(cfg: ManagerConfig, ucfg: UsageLimitConfig, token: str, *,
                 now: datetime, consider_all: bool = False,
                 want_repo: Optional[str] = None, log=lambda m: None,
                 api=monitor.api_request) -> List[Action]:
    """Read-only: list sessions and classify each into its (would-be) action,
    WITHOUT running any investigator, submitting, or persisting. Powers the
    manager-ui 'refresh / detect what's stuck right now' view.

    Uses a fresh (empty) cooldown state so detection reflects the true current
    state of every session, not what the monitor happened to act on recently."""
    sessions = monitor.list_sessions(ucfg, token, log)
    if not sessions:
        return []
    index = build_worktree_index(cfg.dev)
    try:
        allow = load_allowlist(cfg.allowlist_file.read_text())
    except OSError:
        allow = {}
    return collect_actions(sessions, cfg=cfg, ucfg=ucfg, token=token, index=index,
                           allow=allow, consider_all=consider_all, want_repo=want_repo,
                           now=now, now_epoch=now.timestamp(), state={"sessions": {}},
                           log=log, api=api)


def action_row(a: Action) -> dict:
    """A JSON-friendly row describing one classified action for the ui."""
    questions = []
    if a.required:
        questions = [{"header": q.get("header", ""), "question": q.get("question", ""),
                      "options": option_labels(q)} for q in a.required.questions]
    return {"session_id": a.session_id, "repo": a.repo, "case": a.kind,
            "reason": a.reason, "managed": a.managed, "question": a.question,
            "questions": questions, "sig": action_sig(a), "fresh": a.fresh,
            "title": a.title,
            "actionable": a.managed and a.kind not in (SKIP, DEFER)}


def worktree_for(cfg: ManagerConfig, session_id: str) -> Optional[Path]:
    """The local bridge worktree dir for a session, or None (cloud / other host)."""
    try:
        matches = sorted(cfg.dev.glob(f"*/.claude/worktrees/bridge-{session_id}"))
    except OSError:
        return None
    return matches[0] if matches else None


def worktree_status(path: Optional[Path]) -> dict:
    """Best-effort {exists, branch, dirty} for a worktree dir (no network)."""
    if not path or not os.path.isdir(path):
        return {"exists": False}

    def git(*args: str) -> str:
        try:
            p = subprocess.run(["git", "-C", str(path), *args],
                               capture_output=True, text=True, timeout=5)
            return p.stdout if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    porcelain = git("status", "--porcelain")
    return {"exists": True,
            "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "dirty": len([ln for ln in porcelain.splitlines() if ln.strip()])}


def session_transcript(cfg: ManagerConfig, session_id: str) -> Optional[Path]:
    """The newest local transcript JSONL for a session, or None. Bridge transcripts
    are named by a per-session UUID, not the ``cse_`` id -- the id lives in the
    *project dir* (the session's cwd = its worktree, slugified). So we map
    session -> worktree -> ``~/.claude/projects/<slug>/`` -> newest ``*.jsonl``."""
    wt = worktree_for(cfg, session_id)
    if not wt:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt))
    proj = Path.home() / ".claude" / "projects" / slug
    try:
        jsonls = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return jsonls[-1] if jsonls else None


# Slack absorbing the gap between a session's last write and the analysis that
# reacted to it: the recorded ts is floored to the second and the investigator
# takes a moment to run, so an update within a couple seconds of an analysis is
# treated as concurrent, not as the analysis being out of date.
STALE_SLACK_SECS = 2.0


def session_mtime(cfg: ManagerConfig, session_id: str) -> Optional[float]:
    """Epoch mtime of a session's newest local transcript -- its last-updated time
    -- or None when there's no local transcript (cloud / other-host / unmapped)."""
    p = session_transcript(cfg, session_id)
    try:
        return p.stat().st_mtime if p else None
    except OSError:
        return None


def analysis_is_stale(analyzed_at: str, session_mtime: Optional[float],
                      *, slack_secs: float = STALE_SLACK_SECS) -> bool:
    """True when the session's transcript was written *after* the analysis was
    recorded -- i.e. the cached analysis predates the session's current state and
    no longer reflects it. Pure (time injected) so it's unit-testable. Returns
    False whenever we can't tell -- no local transcript or an unparseable ts -- so
    we never flag staleness we can't actually verify."""
    if session_mtime is None or not analyzed_at:
        return False
    try:
        analyzed_epoch = datetime.fromisoformat(analyzed_at).timestamp()
    except ValueError:
        return False
    return session_mtime > analyzed_epoch + slack_secs


def last_messages(path: Optional[Path], n: int = 2, tail_bytes: int = 65536) -> List[dict]:
    """The last *n* user/assistant turns (``[{role, text}]``) of a transcript, read
    from just the file's tail so a multi-MB log stays cheap. Skips meta/command
    wrapper turns and tool-only turns (no text)."""
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            data = fh.read().decode("utf-8", "ignore")
    except OSError:
        return []
    lines = data.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # drop the partial first line from the tail cut
    out: List[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant")
                or obj.get("isMeta")):
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if not text or text.startswith("<"):   # skip command/meta wrappers
            continue
        out.append({"role": obj.get("type"), "text": text})
    return out[-n:]


def read_decisions(cfg: ManagerConfig) -> List[dict]:
    """Parse the append-only decisions JSONL newest-first; tolerant of blank/partial
    lines (the monitor may be mid-append)."""
    out: List[dict] = []
    try:
        text = cfg.decisions_file.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    out.reverse()
    return out


def latest_decision_by_sig(cfg: ManagerConfig) -> Dict[str, dict]:
    """The newest decision record per cache signature -- the analysis cache."""
    by: Dict[str, dict] = {}
    for rec in read_decisions(cfg):  # newest-first, so first seen wins
        sig = rec.get("sig")
        if sig and sig not in by:
            by[sig] = rec
    return by


def analyze_session(cfg: ManagerConfig, ucfg: UsageLimitConfig, token: str,
                    session_id: str, *, now: datetime, guidelines: Optional[str] = None,
                    force: bool = False, log=lambda m: None, runner=subprocess.run,
                    api=monitor.api_request) -> Optional[dict]:
    """Find one session's current action, run the investigator (with guidelines),
    append + return its decision record. Reuses the cached analysis for the same
    situation unless *force*. None if the session isn't a stuck/actionable thread
    right now. The on-demand path behind the ui's Analyze / Re-analyze buttons."""
    if guidelines is None:
        guidelines = read_guidelines(cfg)
    actions = scan_actions(cfg, ucfg, token, now=now, consider_all=True, log=log, api=api)
    a = next((x for x in actions if x.session_id == session_id), None)
    if a is None or not a.managed:
        return None  # unknown / out-of-scope session -- nothing to analyze
    sig = action_sig(a)
    if not force:
        cached = latest_decision_by_sig(cfg).get(sig)
        # Reuse the cache only if it still reflects the session: a transcript
        # written after the analysis means the thread has moved on, so re-run.
        if (cached and cached.get("analyzed")
                and not analysis_is_stale(cached.get("ts", ""),
                                          session_mtime(cfg, session_id))):
            return cached
    advice = analyze_action(a, cfg=cfg, guidelines=guidelines, log=log, runner=runner)
    rec = decision_record(a, advice=advice, ts=now.isoformat(timespec="seconds"))
    append_decision(cfg, rec)
    return rec


def run_monitor(cfg: ManagerConfig, ucfg: UsageLimitConfig, *, log,
                consider_all: bool = False, want_repo: Optional[str] = None,
                get_token=monitor.get_token) -> int:
    """Loop monitor_tick every ``interval_secs`` until SIGTERM/SIGINT."""
    global _running
    _running = True

    def handler(signum, _frame):
        global _running
        log(f"received signal {signum}; stopping")
        _running = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    cfg.logdir.mkdir(parents=True, exist_ok=True)
    log(f"manager monitor up pid={os.getpid()} interval={cfg.interval_secs}s "
        f"submit_enabled={cfg.submit_enabled} decisions={cfg.decisions_file}")
    last = 0.0
    while _running:
        now_t = time.time()
        if now_t - last >= cfg.interval_secs:
            last = now_t
            token = get_token(ucfg, log)
            if token:
                try:
                    monitor_tick(cfg, ucfg, token, now=datetime.now(timezone.utc),
                                 log=log, consider_all=consider_all, want_repo=want_repo)
                except Exception as e:  # noqa: BLE001 (a daemon must stay up)
                    log(f"tick failed: {e}")
        time.sleep(1)
    log("manager monitor exiting")
    return 0


def do_replay(path: str, *, cfg: ManagerConfig, repo: Optional[str], log,
              runner=subprocess.run) -> int:
    """Run the investigator against a SAVED scenario (no live session) and report
    the option it picks -- the offline way to test responses to any scenario."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read scenario {path}: {e}", file=sys.stderr)
        return 2
    ra = RequiredAction(tool_name=data.get("tool_name", ""),
                        tool_use_id=data.get("tool_use_id", ""),
                        description=data.get("description", ""),
                        questions=data.get("questions", []))
    if not ra.questions:
        print("scenario has no questions", file=sys.stderr)
        return 2
    repo = repo or data.get("repo")
    run_dir = str(cfg.dev / repo) if repo else os.getcwd()
    prompt = investigator_prompt_answer(ra, repo=repo, run_dir=run_dir)
    cmd = investigator_command(cfg.claude_bin, prompt, cfg.model,
                               cfg.investigator_permission_mode)
    print(f"replaying {path}\n  repo={repo}  run_dir={run_dir}\n")
    print(format_questions(ra))
    out = run_investigator(cmd, run_dir, cfg.investigator_timeout_secs, runner=runner, log=log)
    print("\n--- investigator output ---")
    print(out or "(no output)")
    sels = parse_choices(out or "", ra)
    print("\n--- parsed selection ---")
    print("; ".join(f"{s['header']}={s['label']}" for s in sels) if sels
          else "(unmappable to valid options)")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control manager [--go] [--all] [--repo REPO] [--json] [--dev DIR]\n"
    "  Watch every session's state and PLAN the manager action for each: answer a\n"
    "  waiting question (the dashboard shows the actual AskUserQuestion options),\n"
    "  review an idle session, or rescue a broken one.\n"
    "  DRY-RUN by default (MANAGER_DRY_RUN=1): a read-only dashboard, runs nothing.\n"
    "  --go   : live -- run the investigator + pick an option for waiting sessions\n"
    "           (review/rescue stay dry-run). The chosen option is only POSTed when\n"
    "           MANAGER_SUBMIT_ENABLED=1 (the tool_result shape is still being\n"
    "           confirmed); otherwise it investigates + reports the pick only.\n"
    "  --all  : consider every repo, not just the allowlist\n"
    "  --repo : only this repo basename (case-insensitive)\n"
    "  --json : machine-readable plan instead of the dashboard (never executes)\n"
    "  --loop : run forever as the monitor -- each interval record every stuck\n"
    "           thread (answer/review/rescue), investigating waiting ones to pick\n"
    "           an option; never submits. Review the log in `manager-ui`.\n"
    "  --replay FILE : run the investigator against a SAVED scenario (no live\n"
    "                  session) and print the option it picks -- offline testing\n"
    "  --dev  : dev root for repo lookup (default ~/dev)"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"all": False, "repo": None, "json": False, "dev": DEV, "go": False,
            "loop": False, "replay": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all":
            opts["all"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--go":
            opts["go"] = True
        elif a == "--loop":
            opts["loop"] = True
        elif a == "--replay":
            i += 1; opts["replay"] = argv[i]
        elif a == "--repo":
            i += 1; opts["repo"] = argv[i]
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a in ("-h", "--help"):
            opts["help"] = True
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    return opts


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE)
        return 0

    cfg = ManagerConfig.from_env()
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731 (API client diagnostics)
    ucfg = UsageLimitConfig.from_env()
    if opts["dev"] != str(cfg.dev):
        cfg = dataclasses.replace(cfg, dev=Path(opts["dev"]))

    # --replay runs offline against a saved scenario; no session list needed.
    if opts["replay"]:
        return do_replay(opts["replay"], cfg=cfg, repo=opts["repo"], log=log)

    want_repo = opts["repo"].lower() if opts["repo"] else None
    # --loop: the monitor (evaluate + report, never submit). Log to a file so the
    # run is reviewable; decisions also go to cfg.decisions_file.
    if opts["loop"]:
        from .logging_util import make_logger
        cfg.logdir.mkdir(parents=True, exist_ok=True)
        flog = make_logger(cfg.log_file, utc=True)
        return run_monitor(cfg, ucfg, log=flog, consider_all=opts["all"],
                           want_repo=want_repo)

    # Live when explicitly asked (--go) or when the daemon switch is off.
    live = opts["go"] or not cfg.dry_run
    token = monitor.get_token(ucfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    sessions = monitor.list_sessions(ucfg, token, log)
    if sessions is None:
        return 1

    index = build_worktree_index(cfg.dev)
    try:
        allow = load_allowlist(cfg.allowlist_file.read_text())
    except OSError:
        allow = {}
    state = read_state(cfg)
    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    actions = collect_actions(sessions, cfg=cfg, ucfg=ucfg, token=token, index=index,
                              allow=allow, consider_all=opts["all"], want_repo=want_repo,
                              now=now, now_epoch=now_epoch, state=state, log=log)

    if opts["json"]:
        # --json is the inspection view: plan only, never executes.
        print(json.dumps([a._asdict() for a in actions], indent=2))
        return 0

    host = socket.gethostname()
    actionable = [a for a in actions if a.managed and a.kind not in (SKIP, DEFER)]
    print(f"host={host}  live={live}  "
          f"{len(actionable)} actionable of {len(actions)} session(s)\n")
    print(format_plan(actions, host, dry_run=not live))
    if actions:
        print(f"\n{summarize(actions)}")
    if not live:
        return 0

    # ---- live execution: answer path only, capped per tick ----
    print("\n---- EXECUTING (live) ----")
    done = 0
    acted = False
    for a in actionable:
        if done >= cfg.max_actions_per_tick:
            print(f"[cap] max-actions-per-tick={cfg.max_actions_per_tick} reached; "
                  f"deferring the rest to the next run")
            break
        if a.kind != ANSWER:
            print(f"[skip-live] {a.session_id}: {a.kind} not wired for live execution "
                  f"yet (answer path only) -- left as plan")
            continue
        ok = execute_answer(a, ucfg=ucfg, token=token, cfg=cfg, state=state,
                            now_epoch=now_epoch, log=log)
        print(f"[{'submitted' if ok else 'not-submitted'}] {a.session_id} "
              f"(see log for the chosen option)")
        done += 1
        acted = acted or ok
    if acted:
        write_state(cfg, state)
    elif done == 0:
        print("(nothing to execute this run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
