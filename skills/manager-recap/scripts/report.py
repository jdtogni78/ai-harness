#!/usr/bin/env python3
"""report.py — manager activity reporter.

Recaps what a manager session delegated and what its workers actually did, then
renders it as a self-contained HTML deck (REUSING cos-console deck.generate) plus
an in-chat markdown recap.

    report.py [manager_cse_id] [--since-last | --since <when> | --all]
              [--outdir DIR] [--slug NAME] [--no-advance] [--json]

Manager id resolution: explicit arg, else own cse_id from
CLAUDE_CODE_SESSION_ACCESS_TOKEN (same path the manage skill uses).

Scope (INCREMENTAL by default):
    --since-last (default)  only events after the previous /report for this
                            manager (marker: <mgr>.report-state.json). Advances
                            the marker unless --no-advance.
    --since <when>          events after <when> (ISO ts, or 'all'). Read-only.
    --all                   the whole state log. Read-only.

Data sources (all already produced by the manage pattern — read-only w.r.t.
every reported system):
  * manager state log  ~/.ai-harness/manager/<mgr>.jsonl (register/update/close)
  * per-worker CHANGES  git log/diff --stat + commit/merge SHA from close reason
  * TESTING evidence    mined from worker notes and CLASSIFIED:
                          VERIFIED  - an evidence pointer the reporter resolved
                                      (test file on disk, test files in the
                                      landed commit, an artifact that exists)
                          CLAIMED   - a bare prose note; self-reported only
                          NONE      - stated explicitly as "no tests run",
                                      never faked or shown as 0
  * ticket states       gh issue view (best-effort; offline -> "n/a")
  * decisions / open questions  from close reasons + un-closed workers

Every worker is reconciled to its LATEST state before rendering: notes from a
superseded status epoch are never surfaced as current, so one deck cannot say
"holding for OK" and "merged" about the same worker.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AH_ROOT = HERE.parents[2]                      # ai-harness/
DECK_DIR = AH_ROOT / "cos-console" / "presentation-poc"
STATE_DIR = Path(os.environ.get("MANAGER_STATE_DIR",
                                Path.home() / ".ai-harness" / "manager"))

sys.path.insert(0, str(DECK_DIR))
sys.path.insert(0, str(AH_ROOT))
from deck.generate import write_generic_deck            # noqa: E402  (reuse, not copy)

# testing evidence: prose that CLAIMS a test happened (necessary, not sufficient)
TEST_RE = re.compile(
    r"\b(tests?|tested|coverage|\d+\s*/\s*\d+|verified|measured|screenshots?|"
    r"suite|passing|ci\b|browser tour)\b", re.I)
SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
ISO = "%Y-%m-%dT%H:%M:%SZ"

# a path-ish token in a note, e.g. tests/test_report.py or verification/x.png
PATH_RE = re.compile(r"[\w./~-]*[\w-]/[\w./-]*\.\w{1,5}\b|\b[\w./-]+\.(?:py|js|ts|tsx|sh|png|jpg|jpeg|html|json|xml|txt|md|log)\b")
# a filename that is itself a test/verification artifact
TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|spec|specs|__tests__|verification|e2e)/|"
    r"(^|/)(test_[\w-]+|[\w-]+_test|[\w-]+\.test|[\w-]+\.spec)\.\w+$", re.I)

# freshness thresholds (seconds) for the source data behind the report
FRESH_WARN_SECS = int(os.environ.get("REPORT_FRESH_WARN_SECS", 24 * 3600))
FRESH_STALE_SECS = int(os.environ.get("REPORT_FRESH_STALE_SECS", 72 * 3600))


# --------------------------------------------------------------------------- #
# identity + marker
# --------------------------------------------------------------------------- #
def resolve_manager_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("MANAGER_CSE_ID"):
        return os.environ["MANAGER_CSE_ID"]
    try:
        from remote_control.session_list import own_session_id_from_env
        sid = own_session_id_from_env(dict(os.environ))
    except Exception:
        sid = None
    if not sid:
        sys.exit("report.py: could not resolve manager cse_id — pass it explicitly "
                 "(report.py cse_...) or set MANAGER_CSE_ID.")
    return sid


def marker_path(mgr: str) -> Path:
    return STATE_DIR / f"{mgr}.report-state.json"


def read_marker(mgr: str) -> dict:
    p = marker_path(mgr)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def advance_marker(mgr: str, now: str) -> None:
    p = marker_path(mgr)
    prev = read_marker(mgr)
    hist = prev.get("history", [])
    hist.append({"reported_at": now, "prev_since": prev.get("last_report_ts")})
    p.write_text(json.dumps({"last_report_ts": now, "history": hist[-20:]},
                            indent=2))


# --------------------------------------------------------------------------- #
# state log
# --------------------------------------------------------------------------- #
def load_events(mgr: str) -> list[dict]:
    p = STATE_DIR / f"{mgr}.jsonl"
    if not p.is_file():
        sys.exit(f"report.py: no state log for manager {mgr} at {p}")
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def fold_workers(events: list[dict]) -> dict[str, dict]:
    """Fold register/update/close into per-worker current state (full history)."""
    workers: dict[str, dict] = {}
    for e in events:
        w = e.get("worker")
        if not w:
            continue
        rec = workers.setdefault(w, {"worker": w, "events": [], "status": "?",
                                     "notes": [], "ticket": None, "dir": None,
                                     "brief": None, "ord": None,
                                     "status_ts": None})
        rec["events"].append(e)
        ev = e.get("event")
        if ev == "register":
            rec.update(ticket=e.get("ticket"), dir=e.get("dir"),
                       brief=e.get("brief"), ord=e.get("worker_ord"),
                       registered_ts=e.get("ts"))
            rec["status"] = "registered"
            rec["status_ts"] = e.get("ts")
        elif ev == "update":
            if e.get("status") and e["status"] != rec["status"]:
                rec["status"] = e["status"]
                rec["status_ts"] = e.get("ts")
            if e.get("note"):
                rec["notes"].append({"note": e["note"], "ts": e.get("ts", "")})
        elif ev == "close":
            rec["status"] = "closed"
            rec["status_ts"] = e.get("ts")
            rec["close_reason"] = e.get("reason")
            rec["closed_ts"] = e.get("ts")
    return workers


def reconcile(rec: dict) -> dict:
    """Split a worker's notes into CURRENT (belonging to its latest status
    epoch) and SUPERSEDED (written before the last status change).

    This is what stops the deck contradicting itself: a "holding for OK to
    commit+push" note written before the worker closed as merged is history,
    not current state, and must never be rendered as the worker's position.
    """
    notes = rec.get("notes", [])
    epoch = rec.get("status_ts") or ""
    current = [n for n in notes if not epoch or n.get("ts", "") >= epoch]
    superseded = [n for n in notes if epoch and n.get("ts", "") < epoch]
    # a status change with no note of its own still has a latest position: the
    # close reason. Keep current non-empty when we can.
    if not current and rec.get("close_reason"):
        current = [{"note": rec["close_reason"], "ts": rec.get("closed_ts", "")}]
    return {"status": rec.get("status", "?"), "status_ts": epoch or None,
            "current": current, "superseded": superseded}


def event_ts(e: dict) -> str:
    return e.get("ts", "")


# --------------------------------------------------------------------------- #
# per-worker gathering
# --------------------------------------------------------------------------- #
def git(dirpath: str, *args) -> tuple[bool, str]:
    if not dirpath or not Path(dirpath).is_dir():
        return False, ""
    try:
        p = subprocess.run(["git", "-C", dirpath, *args],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return False, ""
    if p.returncode != 0:
        return False, p.stderr.strip()
    return True, p.stdout.strip()


def gather_changes(rec: dict) -> dict:
    """Commit/merge SHA (from close reason) + git diff --stat when reachable."""
    sha = None
    reason = rec.get("close_reason") or ""
    m = SHA_RE.search(reason)
    if m:
        sha = m.group(1)
    stat, files, insertions, deletions = None, None, None, None
    ok, out = (False, "")
    if sha:
        ok, out = git(rec.get("dir"), "show", "--stat", "--oneline", sha)
        if not ok:                     # merged commit may live in ai-harness main
            ok, out = git(str(AH_ROOT), "show", "--stat", "--oneline", sha)
    if ok and out:
        stat = out
        sm = re.search(r"(\d+) files? changed", out)
        files = int(sm.group(1)) if sm else None
        im = re.search(r"(\d+) insertions?", out)
        insertions = int(im.group(1)) if im else None
        dm = re.search(r"(\d+) deletions?", out)
        deletions = int(dm.group(1)) if dm else None
    return {"sha": sha, "reason": reason or None, "stat": stat,
            "files": files, "insertions": insertions, "deletions": deletions,
            "git_reachable": bool(stat)}


def changed_files(stat: str | None) -> list[str]:
    """Filenames out of a `git show --stat` block."""
    if not stat:
        return []
    out = []
    for line in stat.splitlines():
        if "|" not in line:
            continue
        name = line.split("|", 1)[0].strip()
        if name:
            out.append(name)
    return out


def resolve_pointer(note: str, workdir: str | None) -> str | None:
    """Return a path from `note` that actually EXISTS on disk, else None.

    This is the whole difference between VERIFIED and CLAIMED: a note may say
    "tested (5/5)" all it likes — unless it hands us something we can go look
    at, it is a self-report.
    """
    for m in PATH_RE.finditer(note or ""):
        cand = m.group(0).strip(".,;:)")
        for base in (workdir, str(AH_ROOT), None):
            p = (Path(base) / cand) if base else Path(cand).expanduser()
            try:
                if p.exists():
                    return str(p)
            except OSError:
                continue
    return None


def gather_testing(rec: dict, recon: dict, changes: dict) -> dict:
    """Classify testing evidence as VERIFIED / CLAIMED / NONE.

    VERIFIED needs an evidence pointer the reporter resolved itself — a path in
    the note that exists on disk, or test files inside the commit the worker
    landed. Anything else that merely *talks* about tests is CLAIMED
    (self-reported). No match at all is NONE — stated as "no tests run", never
    a fabricated 0.
    """
    workdir = rec.get("dir")
    current = [n["note"] for n in recon["current"]]
    older = [n["note"] for n in recon["superseded"]]
    claims = [n for n in current if TEST_RE.search(n)]
    stale_claims = [n for n in older if TEST_RE.search(n)]

    pointers = []
    for note in claims + stale_claims:
        ptr = resolve_pointer(note, workdir)
        if ptr:
            pointers.append(ptr)

    test_files = [f for f in changed_files(changes.get("stat"))
                  if TEST_FILE_RE.search(f)]
    if test_files:
        sha = (changes.get("sha") or "")[:10]
        pointers.extend(f"{sha}:{f}" for f in test_files)

    if pointers:
        state = "verified"
    elif claims or stale_claims:
        state = "claimed"
    else:
        state = "none"
    return {"state": state,
            "verified": state == "verified",
            "claimed": state == "claimed",
            # kept for callers that only asked "did anything mention tests?"
            "has_evidence": state != "none",
            "pointers": pointers,
            "claims": claims,
            "superseded_claims": stale_claims,
            "evidence": claims or stale_claims}


def gather_ticket(rec: dict, repo: str) -> dict:
    tk = rec.get("ticket")
    if not tk:
        return {"number": None, "state": None, "title": None, "reachable": False}
    try:
        p = subprocess.run(
            ["gh", "issue", "view", str(tk), "--repo", repo,
             "--json", "state,title"],
            capture_output=True, text=True, timeout=20)
        if p.returncode == 0:
            d = json.loads(p.stdout)
            return {"number": tk, "state": d.get("state"),
                    "title": d.get("title"), "reachable": True}
    except Exception:
        pass
    return {"number": tk, "state": None, "title": None, "reachable": False}


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def build_report(mgr: str, events: list[dict], cutoff: str | None,
                 repo: str) -> dict:
    workers = fold_workers(events)                    # full history per worker

    # which workers are IN SCOPE = had an event after cutoff (None => all)
    def in_scope(rec: dict) -> bool:
        if cutoff is None:
            return True
        return any(event_ts(e) > cutoff for e in rec["events"])

    scoped = [r for r in workers.values() if in_scope(r)]
    scoped.sort(key=lambda r: (r.get("ord") or 0))

    epics = sorted({e.get("epic") for e in events
                    if e.get("event") == "mgr_epic" and e.get("epic")})

    enriched = []
    for rec in scoped:
        recon = reconcile(rec)
        changes = gather_changes(rec)
        enriched.append({
            **{k: rec.get(k) for k in
               ("worker", "ticket", "dir", "brief", "ord", "status",
                "status_ts", "close_reason", "closed_ts", "registered_ts")},
            "notes": [n["note"] for n in rec.get("notes", [])],
            "reconciled": recon,
            "latest_note": (recon["current"][-1]["note"] if recon["current"]
                            else None),
            "changes": changes,
            "testing": gather_testing(rec, recon, changes),
            "ticket_state": gather_ticket(rec, repo),
        })
    return {"manager": mgr, "repo": repo, "cutoff": cutoff, "epics": epics,
            "workers": enriched, "total_events": len(events),
            "freshness": freshness(events)}


# --------------------------------------------------------------------------- #
# freshness — how old is the data this report is built from?
# --------------------------------------------------------------------------- #
def parse_ts(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def freshness(events: list[dict], now: datetime | None = None) -> dict:
    """Age of the newest source event. A deck that says nothing about its own
    staleness invites being read as live."""
    now = now or datetime.now(timezone.utc)
    stamps = [t for t in (parse_ts(event_ts(e)) for e in events) if t]
    if not stamps:
        return {"latest_event": None, "age_secs": None, "level": "unknown",
                "label": "source data age unknown"}
    latest = max(stamps)
    age = int((now - latest).total_seconds())
    if age >= FRESH_STALE_SECS:
        level = "stale"
    elif age >= FRESH_WARN_SECS:
        level = "aging"
    else:
        level = "fresh"
    return {"latest_event": latest.strftime(ISO), "age_secs": age,
            "level": level, "label": f"newest source event {human_age(age)} old"}


def human_age(secs: int) -> str:
    if secs < 90:
        return f"{secs}s"
    if secs < 90 * 60:
        return f"{secs // 60}m"
    if secs < 48 * 3600:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# --------------------------------------------------------------------------- #
# slide spec (generic deck) — summary -> changes -> testing -> tickets ->
#                             decisions -> open questions
# --------------------------------------------------------------------------- #
def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tile(n, label, na=False) -> str:
    cls = " na" if na else ""
    return (f'<div class="tile{cls}"><div class="n val">{esc(n)}</div>'
            f'<div class="l">{esc(label)}</div></div>')


def nodata(what: str) -> str:
    return (f'<div class="nodata-panel"><div class="big">NO DATA — this is NOT zero</div>'
            f'<p class="sub" style="margin:.6em 0 0">{esc(what)} '
            f'The reporter will <b>not</b> invent a value.</p></div>')


def freshness_banner(fr: dict, generated_at: str) -> str:
    """Always-visible provenance strip: when this deck was made and how old the
    data under it is."""
    level = fr.get("level", "unknown")
    cls = {"fresh": "ok", "aging": "warn", "stale": "fail"}.get(level, "")
    mark = {"fresh": "●", "aging": "▲", "stale": "▲"}.get(level, "?")
    extra = ""
    if level == "stale":
        extra = (' — <b>this deck may not reflect current state; '
                 'regenerate before relying on it</b>')
    elif level == "aging":
        extra = " — source data is over a day old"
    elif level == "unknown":
        extra = " — no timestamps in the source log"
    return (f'<div class="card" style="margin-bottom:14px;padding:10px 14px;font-size:13px">'
            f'<span class="pill {cls}">{mark} {esc(level.upper())}</span> '
            f'generated {esc(generated_at)} · {esc(fr.get("label", ""))}{extra}</div>')


def build_deck(rep: dict, generated_at: str, scope_label: str) -> dict:
    ws = rep["workers"]
    n = len(ws)
    fr = rep.get("freshness", {})
    closed = [w for w in ws if w["status"] == "closed"]
    open_w = [w for w in ws if w["status"] != "closed"]
    verified = [w for w in ws if w["testing"]["state"] == "verified"]
    claimed = [w for w in ws if w["testing"]["state"] == "claimed"]
    untested = [w for w in ws if w["testing"]["state"] == "none"]
    with_sha = [w for w in ws if w["changes"]["sha"]]
    mgr_short = rep["manager"]

    slides = []

    # 1. summary ------------------------------------------------------------
    epics = ", ".join(f"#{e}" for e in rep["epics"]) or "—"
    summary_html = (
        freshness_banner(fr, generated_at)
        + '<div class="grid kpis">'
        + tile(n, "workers in scope")
        + tile(len(closed), "closed / delivered")
        + tile(len(open_w), "still open", na=(len(open_w) == 0))
        + tile(len(verified), "test-VERIFIED", na=(len(verified) == 0))
        + '</div>')
    slides.append({
        "widget": "summary", "kicker": "Manager recap",
        "title": f"{mgr_short} — activity report",
        "subtitle": f"{scope_label} · generated {generated_at} · epic {epics}",
        "html": summary_html,
        "narration": (
            f"Here's what manager {mgr_short} did. {scope_label}. "
            f"{n} worker{'s' if n != 1 else ''} in scope, {len(closed)} closed, "
            f"{len(open_w)} still open, {len(with_sha)} with a recorded commit. "
            f"Testing: {len(verified)} verified against an evidence pointer, "
            f"{len(claimed)} self-reported only, {len(untested)} with nothing. "
            + (f"Note the source data is {human_age(fr['age_secs'])} old."
               if fr.get("level") in ("aging", "stale") else "")),
    })

    # 2. changes ------------------------------------------------------------
    if ws:
        rows = []
        for w in ws:
            c = w["changes"]
            sha = c["sha"]
            if sha:
                stat = (f'{c["files"]}f' if c["files"] is not None else "")
                # a commit can be pure additions or pure deletions — render only
                # the halves git actually reported, never "+17677/-None"
                churn = [f'+{c["insertions"]}' if c["insertions"] is not None else "",
                         f'-{c["deletions"]}' if c["deletions"] is not None else ""]
                churn = "/".join(x for x in churn if x)
                stat += f" {churn}" if churn else ""
                right = f'<span class="pill ok">{esc(sha[:10])}</span>{" " + esc(stat) if stat.strip() else ""}'
            else:
                right = '<span class="pill">no commit recorded</span>'
            latest = w.get("latest_note") or w["status"]
            rows.append(
                f'<li><span class="pill">#{esc(w["ticket"])}</span>'
                f'<span>{esc(w["brief"] or w["worker"])}'
                f'<br><span style="color:var(--muted);font-size:12px">'
                f'latest: {esc(str(latest)[:90])}</span></span>'
                f'<span style="margin-left:auto">{right}</span></li>')
        n_superseded = sum(len(w["reconciled"]["superseded"]) for w in ws)
        changes_html = (
            f'<div class="card"><ul class="clean">{"".join(rows)}</ul>'
            f'<p class="sub" style="margin:.8em 0 0;font-size:13px">Each worker is '
            f'shown at its LATEST state'
            + (f'; {n_superseded} earlier note(s) were superseded and are not '
               'reported as current.' if n_superseded else '.')
            + '</p></div>')
        narr = (f"{len(with_sha)} of {n} workers landed a recorded commit or merge. "
                + ("The rest reported done without a commit SHA in the log. "
                   if len(with_sha) < n else "Every worker landed code. ")
                + (f"{n_superseded} earlier status notes were superseded; "
                   "you're seeing only each worker's latest position."
                   if n_superseded else ""))
    else:
        changes_html = nodata("No workers fell in this report window.")
        narr = "No changes in this window."
    slides.append({"widget": "changes", "kicker": "Delivery",
                   "heading": f"Changes — {len(with_sha)}/{n} landed code",
                   "html": changes_html, "narration": narr})

    # 3. testing (the guardrail) -------------------------------------------
    if ws:
        rows = []
        for w in ws:
            t = w["testing"]
            who = f'#{esc(w["ticket"])} {esc(w["brief"] or w["worker"])}'
            if t["state"] == "verified":
                ptr = "; ".join(t["pointers"])[:120]
                rows.append(f'<li><span class="pill ok">✓ VERIFIED</span><span>{who}</span>'
                            f'<span style="margin-left:auto;color:var(--muted)">'
                            f'evidence: {esc(ptr)}</span></li>')
            elif t["state"] == "claimed":
                ev = "; ".join(t["evidence"])[:100]
                rows.append(f'<li><span class="pill warn">⚠︎ SELF-REPORTED</span><span>{who}</span>'
                            f'<span style="margin-left:auto;color:var(--todo)">'
                            f'claim only, no artifact: “{esc(ev)}”</span></li>')
            else:
                rows.append(f'<li><span class="pill fail">no tests run</span><span>{who}</span>'
                            f'<span style="margin-left:auto;color:var(--nodata)">'
                            f'no testing evidence in state log</span></li>')
        testing_html = (
            '<div class="card"><ul class="clean">' + "".join(rows) + '</ul>'
            '<p class="sub" style="margin:.8em 0 0;font-size:13px">'
            '<b>VERIFIED</b> = the reporter resolved a real artifact (a test file, '
            'a test in the landed commit). <b>SELF-REPORTED</b> = the worker said so '
            'and nothing was checked — a green badge here would vouch for tests '
            'nobody looked at.</p></div>')
        if claimed or untested:
            testing_html += nodata(
                f'{len(claimed)} worker(s) SELF-REPORTED tests with no resolvable '
                f'artifact and {len(untested)} have none at all. Neither is a pass.')
        narr = (f"Of {n} workers, {len(verified)} are verified — I resolved a real "
                f"artifact for each. {len(claimed)} only self-reported: they say "
                "tests passed, but nothing here proves it, so they are amber not "
                f"green. {len(untested)} show no testing at all."
                if (claimed or untested) else
                f"All {n} workers are verified — I resolved a real test artifact for each.")
    else:
        testing_html = nodata("No workers to report testing for.")
        narr = "No testing to report this window."
    slides.append({"widget": "testing", "kicker": "Quality",
                   "heading": (f"Testing — {len(verified)} verified, "
                               f"{len(claimed)} self-reported, {len(untested)} none"),
                   "html": testing_html, "narration": narr})

    # 4. tickets ------------------------------------------------------------
    reachable = [w for w in ws if w["ticket_state"]["reachable"]]
    if reachable:
        rows = []
        for w in reachable:
            ts = w["ticket_state"]
            st = ts["state"] or "?"
            cls = "ok" if st == "CLOSED" else "warn"
            rows.append(f'<li><span class="pill {cls}">{esc(st)}</span>'
                        f'<span>#{esc(ts["number"])} {esc(ts["title"] or "")}</span></li>')
        tickets_html = f'<div class="card"><ul class="clean">{"".join(rows)}</ul></div>'
        n_closed_tk = sum(1 for w in reachable if w["ticket_state"]["state"] == "CLOSED")
        narr = (f"{len(reachable)} tickets resolved on the board, "
                f"{n_closed_tk} closed. This is board state, live from GitHub.")
    else:
        # fall back to manager-side status when gh is unreachable
        rows = [f'<li><span class="pill">{esc(w["status"])}</span>'
                f'<span>#{esc(w["ticket"])} {esc(w["brief"] or w["worker"])}</span></li>'
                for w in ws]
        tickets_html = (f'<div class="card"><ul class="clean">{"".join(rows)}</ul></div>'
                        + nodata("GitHub ticket state was not reachable — showing "
                                 "manager-side status instead of live board state."))
        narr = ("The ticket board wasn't reachable, so I'm showing the manager's "
                "own status log, not live GitHub state.")
    slides.append({"widget": "tickets", "kicker": "Board",
                   "heading": "Tickets", "html": tickets_html, "narration": narr})

    # 5. decisions ----------------------------------------------------------
    decisions = []
    for w in closed:
        if w.get("close_reason"):
            decisions.append(f'#{w["ticket"]}: {w["close_reason"]}')
    if decisions:
        rows = "".join(f'<li>{esc(d)}</li>' for d in decisions[:10])
        extra = (f'<li style="color:var(--muted)">…and {len(decisions)-10} more</li>'
                 if len(decisions) > 10 else "")
        decisions_html = f'<div class="card"><ul class="clean">{rows}{extra}</ul></div>'
        narr = (f"{len(decisions)} delivery decisions in scope, mined from close "
                "reasons — each names the commit or merge that landed it.")
    else:
        decisions_html = nodata("No close/delivery decisions recorded in this window.")
        narr = "No delivery decisions were recorded in this window."
    slides.append({"widget": "decisions", "kicker": "Trail",
                   "heading": f"{len(decisions)} delivery decisions",
                   "html": decisions_html, "narration": narr})

    # 6. open questions -----------------------------------------------------
    oq = []
    for w in open_w:
        oq.append(f'#{w["ticket"]} {w["brief"] or w["worker"]} — status '
                  f'"{w["status"]}", not yet closed')
    for w in claimed:
        oq.append(f'#{w["ticket"]} testing is SELF-REPORTED only — no artifact to '
                  'check; confirm the tests exist and assert the right contract')
    for w in untested:
        oq.append(f'#{w["ticket"]} has no testing evidence — confirm whether '
                  'tests were skipped')
    if fr.get("level") in ("aging", "stale"):
        oq.append(f'source data is {human_age(fr["age_secs"])} old — regenerate '
                  'this report before acting on it')
    if oq:
        body = "".join(f'<div style="font-size:18px">⚠︎ {esc(x)}</div>' for x in oq[:8])
        if len(oq) > 8:
            body += f'<div style="color:var(--muted)">…and {len(oq)-8} more</div>'
    else:
        # NOT "everything is on track" — say only what was actually checked.
        body = ('<div style="font-size:20px;color:var(--done)">✓ No open items in '
                'this window.</div>'
                '<div style="color:var(--muted);font-size:14px;margin-top:8px">'
                'Every worker in scope is closed, and each carries a resolvable '
                'test artifact. This covers only what the reporter can check — '
                'state log, git, and the board. It is not a statement about '
                'overall project health.</div>')
    oq_html = f'<div class="card" style="border-color:rgba(210,153,34,.4)">{body}</div>'
    slides.append({"widget": "open_questions", "kicker": "For you",
                   "heading": f"Open {'question' if len(oq)==1 else 'questions'}",
                   "html": oq_html,
                   "narration": (
                       f"{len(oq)} item{'s' if len(oq) != 1 else ''} need you. "
                       f"{oq[0]} I'm not claiming anything beyond that — this "
                       "report checks the state log, git, and the board, and "
                       "nothing else." if oq else
                       "No open items in this window: every worker in scope is "
                       "closed and each carries a resolvable test artifact. "
                       "That's what I checked — it isn't a claim about overall "
                       "project health.")})

    return {"title": f"{mgr_short} — manager activity report",
            "subtitle": scope_label, "generated_at": generated_at,
            "source": "report skill", "slides": slides}


# --------------------------------------------------------------------------- #
# markdown recap
# --------------------------------------------------------------------------- #
def markdown_recap(rep: dict, scope_label: str, paths: dict,
                   generated_at: str = "") -> str:
    ws = rep["workers"]
    n = len(ws)
    closed = sum(1 for w in ws if w["status"] == "closed")
    verified = [w for w in ws if w["testing"]["state"] == "verified"]
    claimed = [w for w in ws if w["testing"]["state"] == "claimed"]
    untested = [w for w in ws if w["testing"]["state"] == "none"]
    fr = rep.get("freshness", {})
    L = [f"## Manager activity report — `{rep['manager']}`",
         f"*{scope_label} · {n} worker(s) in scope · {closed} closed · "
         f"{len(verified)} verified / {len(claimed)} self-reported / "
         f"{len(untested)} untested*",
         f"*Freshness: **{fr.get('level', 'unknown').upper()}** — "
         f"{fr.get('label', 'unknown')} (generated {generated_at})*",
         ""]
    if fr.get("level") == "stale":
        L.append("> ⚠︎ **STALE** — the source data is old; regenerate before "
                 "relying on this.\n")
    if rep["epics"]:
        L.append(f"Epic(s): {', '.join('#'+e for e in rep['epics'])}\n")
    L.append("| # | worker brief | latest state | commit | testing |")
    L.append("|---|---|---|---|---|")
    for w in ws:
        sha = w["changes"]["sha"] or "—"
        t = w["testing"]
        if t["state"] == "verified":
            test = "**VERIFIED** — " + "; ".join(t["pointers"])[:60]
        elif t["state"] == "claimed":
            test = "⚠︎ *self-reported* — " + "; ".join(t["evidence"])[:50]
        else:
            test = "**no tests run**"
        latest = (w.get("latest_note") or w["status"])[:60]
        L.append(f"| #{w['ticket']} | {w['brief'] or w['worker']} | "
                 f"{w['status']} — {latest} | `{sha[:10]}` | {test} |")
    if claimed:
        L.append("")
        L.append("> ⚠︎ **Self-reported, not verified:** " + ", ".join(
            f"#{w['ticket']}" for w in claimed) +
            " claim tests but point at no artifact the reporter could resolve. "
            "Not shown as passing.")
    if untested:
        L.append("")
        L.append("> ⚠︎ **Anti-fabrication:** " + ", ".join(
            f"#{w['ticket']}" for w in untested) +
            " have no testing evidence — reported as *no tests run*, not as 0/passing.")
    L.append("")
    L.append(f"**Deck:** `{paths['html']}` · **narration:** `{paths['narration']}` "
             f"· **demo yaml:** `{paths['demo']}`")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manager", nargs="?", help="manager cse_id (default: self)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--since-last", action="store_true",
                   help="only events since the previous report (default)")
    g.add_argument("--since", metavar="WHEN", help="ISO ts (or 'all')")
    g.add_argument("--all", action="store_true", help="whole state log")
    ap.add_argument("--repo", default="jdtogni78/ai-harness")
    ap.add_argument("--outdir", default=None, help="deck output dir")
    ap.add_argument("--slug", default=None, help="output basename")
    ap.add_argument("--no-advance", action="store_true",
                    help="don't advance the since-last marker")
    ap.add_argument("--json", action="store_true", help="also print the raw report JSON")
    args = ap.parse_args(argv)

    mgr = resolve_manager_id(args.manager)
    events = load_events(mgr)
    now = now_utc()

    # scope
    if args.all or args.since == "all":
        cutoff, scope_label, advance = None, "full history", False
    elif args.since:
        cutoff, scope_label, advance = args.since, f"since {args.since}", False
    else:  # --since-last (default)
        marker = read_marker(mgr)
        cutoff = marker.get("last_report_ts")
        scope_label = (f"since last report ({cutoff})" if cutoff
                       else "first report — full history")
        advance = not args.no_advance

    rep = build_report(mgr, events, cutoff, args.repo)

    slug = args.slug or f"{mgr}-report"
    outdir = args.outdir or str(DECK_DIR / "out")
    deck = build_deck(rep, now, scope_label)
    paths = write_generic_deck(deck, outdir=outdir, slug=slug)

    if advance:
        advance_marker(mgr, now)

    print(markdown_recap(rep, scope_label, paths, now))
    if args.json:
        print("\n<!-- raw report -->")
        print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
