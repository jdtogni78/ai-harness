---
name: report
description: >-
  Reporter the manager delegates to ("reporter, tell me what happened"). Given
  ANY manager's cse_id, it recaps that manager's recent worker activity —
  per-worker git changes, testing evidence, ticket states, delivery decisions,
  open questions — and presents it as a self-contained HTML deck (REUSING the
  cos-console deck.generate renderer) plus an in-chat markdown recap. Scope is
  INCREMENTAL by default (`--since-last`, backed by a per-manager marker), with
  `--since <when>` / `--all` overrides. Anti-fabrication: missing tests are
  stated explicitly ("no tests run"), never faked or shown as 0. Use when the
  user says "report on manager X", "recap what my workers did", "generate an
  activity report / deck", "what happened since the last report", "reporter,
  tell me what happened", or asks the manager to summarize its roster.
---

# /report — manager activity reporter

A **reporter** the [[manage]]r delegates to. It reads a manager's own state log
plus the systems that log already points at, and turns "what did my workers
do?" into an **HTML deck + markdown recap**. It is **read-only w.r.t. every
reported system** — it never mutates a worker repo, a ticket, or the manager
log; its only write is its own per-manager report marker.

## Invocation

```bash
python3 skills/report/scripts/report.py [manager_cse_id] \
    [--since-last | --since <when> | --all] \
    [--outdir DIR] [--slug NAME] [--no-advance] [--json]
```

- **`manager_cse_id`** — omit to self-detect the calling manager from
  `CLAUDE_CODE_SESSION_ACCESS_TOKEN` (same path [[manage]] uses); pass one to
  report on any other manager.
- Prints a **markdown recap** to stdout and writes the deck under `--outdir`
  (default `cos-console/presentation-poc/out/`): `<slug>.html`,
  `<slug>.narration.json`, `<slug>.demo.yaml` (slug default `<mgr>-report`).

## Scope — INCREMENTAL by default (operator decision)

| flag | window | marker |
|------|--------|--------|
| `--since-last` *(default)* | events after the previous report for this manager | reads + **advances** `<mgr>.report-state.json` |
| `--since <when>` | events after `<when>` (ISO ts, or `all`) | read-only |
| `--all` | the whole state log | read-only |

The marker (`~/.ai-harness/manager/<mgr>.report-state.json`) stores
`last_report_ts` + a short history. First `--since-last` run has no marker, so
it reports full history and then records the marker. Pass `--no-advance` to
report the delta without moving the marker (dry-run).

## Data sources (all already produced by the manage pattern)

- **Manager state log** `~/.ai-harness/manager/<mgr>.jsonl` — register / update /
  close events folded into per-worker current state (ticket #, brief, status,
  notes, close reason).
- **Per-worker CHANGES** — commit/merge SHA parsed from the close reason, plus
  `git show --stat` when the worker dir (or ai-harness main) is reachable.
- **TESTING evidence** — mined from worker notes and **classified**, because a
  note is a claim, not proof:

  | badge | colour | means |
  |---|---|---|
  | **✓ VERIFIED** | green | the reporter resolved a real artifact itself — a path in the note that exists on disk, or test files inside the commit the worker landed. The pointer is printed next to the badge. |
  | **⚠︎ SELF-REPORTED** | amber | the worker said tests passed and nothing was checked. A green badge here would vouch for tests nobody looked at (this is exactly how #105's "tested (5/5)" came to cover tests asserting the wrong contract). |
  | **no tests run** | red | no testing mentioned at all. **Anti-fabrication:** stated on its own line and in a NO-DATA panel — never omitted, never a fabricated `0`/passing. |

  VERIFIED means *the evidence exists and here it is* — it does **not** mean the
  tests assert the right thing. That judgment stays with the reader, which is
  why the pointer is always shown.
- **Ticket states** — `gh issue view` (best-effort; if GitHub is unreachable it
  falls back to the manager-side status and says so).
- **Decisions / open questions** — decisions from close reasons; open questions
  from un-closed workers and any worker missing testing evidence.

## Presentation — HTML deck reusing deck.generate (operator decision)

The deck is rendered by **importing** `cos-console/presentation-poc/deck.generate`
(`write_generic_deck`) — the renderer is **reused, not duplicated**. That module
was generalized to accept a generic `slides` list (each slide `{widget, kicker,
title|heading, subtitle, html, narration}`) rendered through the *same* template
chrome (CSS, hash-router nav, `.tile`/`.card`/`.clean`/`.nodata-panel` widgets,
`narration.json` + narrate-demo `.demo.yaml` companions) as the StatusReport
deck. Slide order: **summary → changes → testing → tickets → decisions → open
questions**.

A narrated MP4 is optional: the emitted `<slug>.demo.yaml` is a ready
[[narrate-demo]] script — `narrate render <slug>.demo.yaml`.

## How to run it (manager flow)

1. Run the script for the target manager (self by default). Read the markdown
   recap it prints — that's the fast path you relay to the boss.
2. Point the boss at the deck (`<slug>.html`, opens standalone in a browser).
   Screenshot with headless Chrome if a still is wanted:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
     --disable-gpu --hide-scrollbars --window-size=1440,900 \
     --virtual-time-budget=1600 --screenshot=out.png "file://…/<slug>.html#2"
   ```
   (`#N` deep-links a slide — `#2` is the testing slide.)
3. Default `--since-last` advances the marker, so the next report is naturally
   incremental. Use `--all` for a full retrospective.

## Reconciliation — latest state wins

Every worker is folded to its **latest status epoch** before rendering. Notes
written before the last status change are **superseded** and never surfaced as
current position; the deck says how many were set aside. This is what stops one
deck reading "holding for OK to commit+push" on the testing slide and "merged
f9b3e6c → origin/main" on the decisions slide about the same worker.

## Freshness

The summary slide carries a provenance strip — generated-at plus the age of the
newest source event — badged **FRESH** / **AGING** (>24h) / **STALE** (>72h),
tunable via `REPORT_FRESH_WARN_SECS` / `REPORT_FRESH_STALE_SECS`. Aging and
stale data also raise an open question telling the reader to regenerate. A deck
with no visible age gets read as live; a 2-day-old one silently misleads.

## Rules

- **Read-only** w.r.t. every reported system; the only write is the report
  marker for `--since-last`.
- **Never fabricate.** Missing testing evidence is "no tests run", missing git a
  "no commit recorded", unreachable GitHub a stated fallback — never a `0`.
- **Never vouch for what wasn't checked.** A worker's claim renders as
  SELF-REPORTED, not as a pass.
- **No unverifiable health claims.** The reporter never says "everything's on
  track" — it states what it checked (state log, git, board) and scopes its
  conclusion to that. Prefer stating what IS known over a summary judgment.
- **Reconcile before rendering** — one worker, one current state, no
  contradictions across slides.
- **Reuse the renderer** — import `deck.generate`; do not copy the template.
