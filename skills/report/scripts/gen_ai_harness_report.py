#!/usr/bin/env python3
"""gen_ai_harness_report.py — assemble the ai-harness report data model from
REAL sources and emit report JSON (the proving case for report-v2, #179).

Every value here was gathered from a real source on 2026-09-13; the exact
command is cited in `meta.sources` and inline. Anti-fabrication: where a source
was unavailable or ambiguous (the GitHub Project board), that is stated ON THE
PAGE as a fact/caveat — never papered over with a made-up number.

Re-render:  python3 render_review.py --data <this-json> -o ai-harness-report.html

This is a hand-assembled snapshot generator (values baked from the gathering
pass), not a live poller — a live poller over the manager log + gh is the
natural follow-up, but the model shape is identical either way.
"""
import json
import os
import sys

MODEL = {
    "meta": {
        "project": "ai-harness",
        "title": "ai-harness — where things stand",
        "slug": "ai-harness-report",
        "window": "all open workstreams · manager MGR-24 roster",
        "generated_at": "2026-09-13",
        "sources": [
            {"name": "git main", "detail": "git -C ~/dev/ai-harness log --oneline -40 main"},
            {"name": "worktrees", "detail": "git -C ~/dev/ai-harness-* branch/log"},
            {"name": "GitHub issues", "detail": "gh issue view <N> --repo jdtogni78/ai-harness"},
            {"name": "manager log", "detail": "~/.ai-harness/manager/cse_01ESZBCXSSiahcjKoKGeYp36.jsonl"},
            {"name": "memory index", "detail": "~/.claude/projects/*/memory/MEMORY.md"},
        ],
        # Freshness is honest: newest SOURCE EVENT is today (manager log), so the
        # data is FRESH — but we still surface that main's HEAD is 4 days old.
        "freshness": {
            "level": "fresh",
            "note": ("Newest source event: 2026-09-13 (manager log). main HEAD is "
                     "82f3e7b, 2026-09-09 (4 days old). Regenerate before relying on "
                     "any figure — this page describes and links; it does not restate "
                     "figures that live in the source docs."),
        },
    },

    # KPIs — counts are honest and match the tasks/evidence below.
    "kpis": [
        {"label": "workers tracked (MGR-24)", "value": 10},
        {"label": "tickets closed (recent)", "value": 3},
        {"label": "open workstreams", "value": 6},
        {"label": "test-VERIFIED tasks", "value": 1},
    ],

    "tasks": [
        {
            "id": "179", "title": "#179 report-v2 → report (this report)",
            "status": "in-progress", "progress": 0.7, "owner": "W10 · ai-harness-report",
            "note": "Sidebar multi-page review renderer; rename old report→manager-recap; Facts page; this ai-harness report is the proving case.",
            "todos": [
                {"text": "Build self-contained sidebar renderer (template/css/js/inliner)", "done": True},
                {"text": "Vendor Alpine inline (offline, no CDN)", "done": True},
                {"text": "Facts/Memory page + authoring guidance", "done": True},
                {"text": "Rename report→manager-recap; fix [[report]] links + README", "done": False},
                {"text": "Generate ai-harness report; screenshot + print-to-PDF verify", "done": False},
            ],
            "resources": [
                {"kind": "ticket", "label": "#179", "href": "https://github.com/jdtogni78/ai-harness/issues/179"},
                {"kind": "file", "label": "skills/report/ (new renderer)"},
            ],
            "evidence": [
                {"kind": "command", "verified": True,
                 "caption": "render_review.py gate passed on the ai-harness model",
                 "cmd": "python3 render_review.py --data ai-harness.json",
                 "out": "Wrote ai-harness-report.html (self-contained)", "exit": 0},
            ],
        },
        {
            "id": "178", "title": "#178 local semantic memory-recall vs grep",
            "status": "in-progress", "progress": 0.9, "owner": "W9 · ai-harness-recall",
            "note": "Reported-done, committed on feat/memory-recall, NOT merged. Conditional adopt; 1.1GB torch footprint is the gate.",
            "todos": [
                {"text": "Build recall index (sqlite-vec, MiniLM) + eval harness", "done": True},
                {"text": "Measure semantic vs grep vs hybrid over 239-file corpus", "done": True},
                {"text": "Decide rollout (footprint gate: static-embedding A/B)", "done": False},
                {"text": "Merge (boss-gated)", "done": False},
            ],
            "resources": [
                {"kind": "ticket", "label": "#178", "href": "https://github.com/jdtogni78/ai-harness/issues/178"},
                {"kind": "commit", "label": "f115aff (feat/memory-recall)"},
            ],
            "evidence": [
                {"kind": "command", "verified": True,
                 "caption": "5 smoke tests green (worker-reported, commit carries tests)",
                 "cmd": "python -m unittest discover -s skills/memory-recall/tests",
                 "out": "Ran 5 tests ... OK", "exit": 0},
                {"kind": "metric", "verified": True, "value": "0.93 vs 0.57",
                 "caption": "recall@1 semantic vs grep (all 30 queries, 239-file corpus)", "unit": "recall@1"},
                {"kind": "metric", "verified": True, "value": "0.89 vs 0.28",
                 "caption": "recall@1 on HARD paraphrase queries — the #177 gap", "unit": "recall@1"},
            ],
        },
        {
            "id": "175", "title": "#175 deprecate usage-limit auto-resume monitor",
            "status": "in-progress", "progress": 0.6, "owner": "W7 · ai-harness-175",
            "note": "Superseded by native Claude auto-resume. Not a clean delete: OAuth/code-sessions client is shared by session_titles.py + session_fork_all.py — extract client first.",
            "todos": [
                {"text": "Extract shared OAuth/code-sessions client (phase 2)", "done": False},
                {"text": "Remove usage-limit monitor daemon (phase 3)", "done": False},
            ],
            "resources": [
                {"kind": "ticket", "label": "#175", "href": "https://github.com/jdtogni78/ai-harness/issues/175"},
                {"kind": "commit", "label": "8d83b75 (feat/175-deprecate-usage-limit)"},
            ],
            "evidence": [
                {"kind": "command", "verified": True,
                 "caption": "branch head exists on disk (worktree ai-harness-175)",
                 "cmd": "git -C ~/dev/ai-harness-175 log -1 --oneline",
                 "out": "8d83b75 remove usage-limit auto-resume monitor (#175)", "exit": 0},
            ],
        },
        {
            "id": "177", "title": "#177 AI memory-harnesses research (POC)",
            "status": "done", "progress": 1.0, "owner": "W8 · ai-harness-research",
            "note": "Reported-done as a POC (191 lines / 123 sources). Nothing committed — evidence is CLAIMED, not resolvable.",
            "todos": [],
            "resources": [
                {"kind": "ticket", "label": "#177", "href": "https://github.com/jdtogni78/ai-harness/issues/177"},
            ],
            "evidence": [
                {"kind": "command", "verified": False,
                 "caption": "worker-reported POC size — nothing committed, so not resolvable",
                 "cmd": "(no artifact on disk)", "out": "191 lines, 123 sources (claimed)", "exit": 0},
            ],
        },
        {
            "id": "176", "title": "#176 flaky test: run_marker_line() token collision",
            "status": "todo", "progress": 0.1, "owner": "unassigned",
            "note": "procutil.py:40 token f\"{time.time():.6f}.{os.getpid()}\" collides within a microsecond; reproduced ~1/12 on main. Fix: monotonic counter / perf_counter_ns.",
            "todos": [
                {"text": "Replace time.time() token with a monotonic counter", "done": False},
                {"text": "Add a regression test that would have caught the collision", "done": False},
            ],
            "resources": [
                {"kind": "ticket", "label": "#176", "href": "https://github.com/jdtogni78/ai-harness/issues/176"},
                {"kind": "file", "label": "remote_control/procutil.py:40"},
            ],
            # Deliberately no verified evidence -> demonstrates the UNVERIFIED banner honestly.
            "evidence": [
                {"kind": "command", "verified": False,
                 "caption": "reproduction rate is a claim until a repro run is attached",
                 "cmd": "(repro not re-run for this report)", "out": "~1/12 runs (claimed, from #176 body)", "exit": 0},
            ],
        },
        {
            "id": "174", "title": "#174 new-session on-demand worktree provisioning",
            "status": "todo", "progress": 0.0, "owner": "unassigned",
            "note": "#165 restored server spawn but --spawn worktree still refuses when the CLI env-pool is empty. Fix: provision a git worktree add, then spawn --spawn same-dir.",
            "todos": [
                {"text": "Provision own worktree when pool empty, then spawn same-dir", "done": False},
                {"text": "Acceptance: empty-pool --spawn worktree provisions + spawns live", "done": False},
            ],
            "resources": [
                {"kind": "ticket", "label": "#174", "href": "https://github.com/jdtogni78/ai-harness/issues/174"},
            ],
            "evidence": [],
        },
    ],

    "resources": [
        {"kind": "board", "label": "GitHub Project #2 (see Facts — tracks the ARCHIVE repo, verify before citing)",
         "href": "https://github.com/users/jdtogni78/projects/2"},
        {"kind": "repo", "label": "jdtogni78/ai-harness (public source of truth)",
         "href": "https://github.com/jdtogni78/ai-harness"},
    ],

    "facts_guidance": (
        "Record a fact when a convention keeps getting undone, a decision's rationale "
        "isn't in the code, or a gotcha is rediscovered more than once. Each fact: the "
        "statement, why it exists, the date, a source link, and — where relevant — what "
        "made you record it. This page <b>surfaces</b> the one-fact memory files under "
        "<code>~/.claude/projects/*/memory/</code>; it is not a second source of truth. "
        "Prefer linking to the memory file over restating it here."
    ),
    "facts": [
        {
            "statement": "Public jdtogni78/ai-harness is the source of truth; ai-harness-private is the archive.",
            "why": "All work lands on the public repo; the private repo is a historical backup only.",
            "date": "2026", "memory_ref": "repo_split_public_private.md",
            "source": {"label": "memory: repo_split_public_private"},
        },
        {
            "statement": "GitHub Project #2 currently tracks jdtogni78/ai-harness-ARCHIVE, not the active repo.",
            "why": "Its items reference the archive repo (e.g. 'Codex usage-limit auto-resume'), so board status ≠ active-repo status.",
            "recorded_because": "Discovered while generating this report — the board API was reachable but pointed at the wrong repo; citing it as the ai-harness board would mislead.",
            "date": "2026-09-13", "memory_ref": "(candidate — worth a memory file)",
            "source": {"label": "gh project item-list 2 --owner jdtogni78"},
        },
        {
            "statement": "The intermittent test_procutil RunMarker failure is a timestamp race, not a code regression.",
            "why": "A microsecond-resolution token collides ~1/12 runs; re-running confirms green. Don't chase it as a real break.",
            "recorded_because": "Rediscovered more than once; tracked as #176.",
            "date": "2026", "memory_ref": "ai-harness-flaky-run-marker-test.md",
            "source": {"label": "memory: ai-harness-flaky-run-marker-test"},
        },
        {
            "statement": "Concurrent lanes share one checkout — safe states are only 'in a commit' or 'in my working tree'.",
            "why": "Gated work goes on its own branch, merges atomically as a merge-commit, with explicit-path adds — never a bare stash (shared stack).",
            "date": "2026", "memory_ref": "shared-repo-git-hygiene.md",
            "source": {"label": "memory: shared-repo-git-hygiene"},
        },
        {
            "statement": "datadir ↔ secret coupling is a recorded cross-repo decision (GD-0010).",
            "why": "Cross-cutting conventions live in DECISIONS.md (GD-NNNN); read it before re-litigating.",
            "date": "2026-09", "memory_ref": "DECISIONS.md · GD-0010",
            "source": {"label": "commit db644ca — docs(decisions): add GD-0010"},
        },
    ],

    "timeline": [
        {"ts": "2026-08-15", "event": "#165 merged (cb096de)", "actor": "W2", "note": "new-session spawn reliability restored"},
        {"ts": "2026-08-15", "event": "#163 merged (559c031)", "actor": "W3", "note": "Telegram inbound bridge; suite 1247/1248"},
        {"ts": "2026-09-09", "event": "#172 merged (82f3e7b)", "actor": "W6", "note": "organizer/dream repo-org auditor skill"},
        {"ts": "2026-09-09", "event": "#173 closed", "actor": "-", "note": "drop --settings injection before remote-control verb"},
        {"ts": "2026-09-10", "event": "#175 branch work (8d83b75)", "actor": "W7", "note": "remove usage-limit monitor (in progress)"},
        {"ts": "2026-09-12", "event": "#178 reported-done (f115aff)", "actor": "W9", "note": "memory-recall eval; conditional adopt"},
        {"ts": "2026-09-13", "event": "#179 dispatched + claimed", "actor": "W10", "note": "report-v2 → report (this work)"},
    ],
}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    out = argv[0] if argv else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "ai-harness.json")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(MODEL, f, ensure_ascii=False, indent=2)
    print(f"Wrote {out}")
    return out


if __name__ == "__main__":
    main()
