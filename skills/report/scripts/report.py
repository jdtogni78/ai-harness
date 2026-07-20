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
  * TESTING evidence    mined from worker notes; missing tests are stated
                        explicitly ("no tests run"), never faked or shown as 0
  * ticket states       gh issue view (best-effort; offline -> "n/a")
  * decisions / open questions  from close reasons + un-closed workers
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

# testing evidence: what counts as "a test was actually run"
TEST_RE = re.compile(
    r"\b(tests?|tested|coverage|\d+\s*/\s*\d+|verified|measured|screenshots?|"
    r"suite|passing|ci\b|browser tour)\b", re.I)
SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
ISO = "%Y-%m-%dT%H:%M:%SZ"


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
                                     "brief": None, "ord": None})
        rec["events"].append(e)
        ev = e.get("event")
        if ev == "register":
            rec.update(ticket=e.get("ticket"), dir=e.get("dir"),
                       brief=e.get("brief"), ord=e.get("worker_ord"),
                       registered_ts=e.get("ts"))
            rec["status"] = "registered"
        elif ev == "update":
            if e.get("status"):
                rec["status"] = e["status"]
            if e.get("note"):
                rec["notes"].append(e["note"])
        elif ev == "close":
            rec["status"] = "closed"
            rec["close_reason"] = e.get("reason")
            rec["closed_ts"] = e.get("ts")
    return workers


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


def gather_testing(rec: dict) -> dict:
    """Mine testing evidence from notes. Anti-fabrication: no match => explicit
    'no tests run', NEVER a fabricated 0."""
    hits = [n for n in rec.get("notes", []) if TEST_RE.search(n)]
    return {"has_evidence": bool(hits), "evidence": hits}


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
        enriched.append({
            **{k: rec.get(k) for k in
               ("worker", "ticket", "dir", "brief", "ord", "status",
                "close_reason", "closed_ts", "registered_ts")},
            "notes": rec.get("notes", []),
            "changes": gather_changes(rec),
            "testing": gather_testing(rec),
            "ticket_state": gather_ticket(rec, repo),
        })
    return {"manager": mgr, "repo": repo, "cutoff": cutoff, "epics": epics,
            "workers": enriched, "total_events": len(events)}


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


def build_deck(rep: dict, generated_at: str, scope_label: str) -> dict:
    ws = rep["workers"]
    n = len(ws)
    closed = [w for w in ws if w["status"] == "closed"]
    open_w = [w for w in ws if w["status"] != "closed"]
    tested = [w for w in ws if w["testing"]["has_evidence"]]
    untested = [w for w in ws if not w["testing"]["has_evidence"]]
    with_sha = [w for w in ws if w["changes"]["sha"]]
    mgr_short = rep["manager"]

    slides = []

    # 1. summary ------------------------------------------------------------
    epics = ", ".join(f"#{e}" for e in rep["epics"]) or "—"
    summary_html = (
        '<div class="grid kpis">'
        + tile(n, "workers in scope")
        + tile(len(closed), "closed / delivered")
        + tile(len(open_w), "still open", na=(len(open_w) == 0))
        + tile(len(with_sha), "with commit/merge")
        + '</div>')
    slides.append({
        "widget": "summary", "kicker": "Manager recap",
        "title": f"{mgr_short} — activity report",
        "subtitle": f"{scope_label} · generated {generated_at} · epic {epics}",
        "html": summary_html,
        "narration": (
            f"Here's what manager {mgr_short} did. {scope_label}. "
            f"{n} worker{'s' if n != 1 else ''} in scope, {len(closed)} closed, "
            f"{len(open_w)} still open. "
            + (f"{len(tested)} carry testing evidence; {len(untested)} do not — "
               "I'll flag those explicitly."
               if untested else "Every worker in scope carries testing evidence.")),
    })

    # 2. changes ------------------------------------------------------------
    if ws:
        rows = []
        for w in ws:
            c = w["changes"]
            sha = c["sha"]
            if sha:
                stat = (f'{c["files"]}f' if c["files"] is not None else "")
                stat += (f' +{c["insertions"]}/-{c["deletions"]}'
                         if c["insertions"] is not None else "")
                right = f'<span class="pill ok">{esc(sha[:10])}</span>{" " + esc(stat) if stat.strip() else ""}'
            else:
                right = '<span class="pill">no commit recorded</span>'
            rows.append(
                f'<li><span class="pill">#{esc(w["ticket"])}</span>'
                f'<span>{esc(w["brief"] or w["worker"])}</span>'
                f'<span style="margin-left:auto">{right}</span></li>')
        changes_html = f'<div class="card"><ul class="clean">{"".join(rows)}</ul></div>'
        narr = (f"{len(with_sha)} of {n} workers landed a recorded commit or merge. "
                + ("The rest reported done without a commit SHA in the log."
                   if len(with_sha) < n else "Every worker landed code."))
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
            if t["has_evidence"]:
                ev = "; ".join(t["evidence"])[:120]
                rows.append(f'<li><span class="pill ok">tested</span>'
                            f'<span>#{esc(w["ticket"])} {esc(w["brief"] or w["worker"])}</span>'
                            f'<span style="margin-left:auto;color:var(--muted)">{esc(ev)}</span></li>')
            else:
                rows.append(f'<li><span class="pill fail">no tests run</span>'
                            f'<span>#{esc(w["ticket"])} {esc(w["brief"] or w["worker"])}</span>'
                            f'<span style="margin-left:auto;color:var(--nodata)">no testing evidence in state log</span></li>')
        testing_html = f'<div class="card"><ul class="clean">{"".join(rows)}</ul></div>'
        if untested:
            testing_html += nodata(
                f'{len(untested)} worker(s) have NO testing evidence — reported as '
                '"no tests run", not as passing.')
        narr = (f"{len(tested)} of {n} workers show testing evidence. "
                + (f"{len(untested)} show none — that's an open item, not a green light."
                   if untested else "None are missing evidence."))
    else:
        testing_html = nodata("No workers to report testing for.")
        narr = "No testing to report this window."
    slides.append({"widget": "testing", "kicker": "Quality",
                   "heading": f"Testing — {len(tested)}/{n} with evidence",
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
    for w in untested:
        oq.append(f'#{w["ticket"]} has no testing evidence — confirm whether '
                  'tests were skipped')
    if oq:
        body = "".join(f'<div style="font-size:18px">⚠︎ {esc(x)}</div>' for x in oq[:8])
        if len(oq) > 8:
            body += f'<div style="color:var(--muted)">…and {len(oq)-8} more</div>'
    else:
        body = ('<div style="font-size:20px;color:var(--done)">✓ Nothing needs you — '
                'every worker in scope is closed and evidenced.</div>')
    oq_html = f'<div class="card" style="border-color:rgba(210,153,34,.4)">{body}</div>'
    slides.append({"widget": "open_questions", "kicker": "For you",
                   "heading": f"Open {'question' if len(oq)==1 else 'questions'}",
                   "html": oq_html,
                   "narration": (f"{oq[0]} Everything else is on track." if oq
                                 else "Nothing needs you right now — everything's on track.")})

    return {"title": f"{mgr_short} — manager activity report",
            "subtitle": scope_label, "generated_at": generated_at,
            "source": "report skill", "slides": slides}


# --------------------------------------------------------------------------- #
# markdown recap
# --------------------------------------------------------------------------- #
def markdown_recap(rep: dict, scope_label: str, paths: dict) -> str:
    ws = rep["workers"]
    n = len(ws)
    closed = sum(1 for w in ws if w["status"] == "closed")
    tested = sum(1 for w in ws if w["testing"]["has_evidence"])
    L = [f"## Manager activity report — `{rep['manager']}`",
         f"*{scope_label} · {n} worker(s) in scope · {closed} closed · "
         f"{tested}/{n} with testing evidence*", ""]
    if rep["epics"]:
        L.append(f"Epic(s): {', '.join('#'+e for e in rep['epics'])}\n")
    L.append("| # | worker brief | status | commit | testing |")
    L.append("|---|---|---|---|---|")
    for w in ws:
        sha = w["changes"]["sha"] or "—"
        test = "; ".join(w["testing"]["evidence"])[:60] if w["testing"]["has_evidence"] \
            else "**no tests run**"
        L.append(f"| #{w['ticket']} | {w['brief'] or w['worker']} | "
                 f"{w['status']} | `{sha[:10]}` | {test} |")
    untested = [w for w in ws if not w["testing"]["has_evidence"]]
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

    print(markdown_recap(rep, scope_label, paths))
    if args.json:
        print("\n<!-- raw report -->")
        print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
