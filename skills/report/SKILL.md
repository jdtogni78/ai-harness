---
name: report
description: >-
  The report-v2 REVIEW renderer: turn a report data model (embedded JSON) into
  ONE self-contained, offline, emailable .html — a sidebar, multi-page,
  one-page-at-a-time review surface (Progress · To-dos · Resources · Validation
  · Facts/Memory · Timeline · Data) that acts as a VALIDATION SURROGATE (judge
  "is this working / on track?" without opening the product). Source keeps
  template / css / js / data as four files; render_review.py inlines them into
  one deliverable; to_pdf.sh prints it headless to PDF. Anti-fabrication:
  verified (resolvable pointer) vs claimed (bare assertion) is shown at a
  glance, a zero is stated not faked, and a task with no proof renders a loud
  UNVERIFIED banner. Use when the user says "generate a review report / status
  page", "make a self-contained progress report", "a report I can open offline /
  email / print to PDF", "show verified vs claimed evidence", or wants the
  report-v2 renderer. (The older manager-activity recap deck is now
  [[manager-recap]].)
---

# /report — report-v2 review renderer

A **review surface**, not a slideshow. Given a report **data model** (JSON), it
emits ONE self-contained `.html` you can open offline, email, archive, or print
to PDF. The reviewer judges *"is this working / on track?"* from embedded
evidence — screenshots, rendered outputs, command results, diffs, metrics —
**without launching the product**. Same honesty stance as [[validate]]:
**verified** (a resolvable pointer someone can open) vs **claimed** (a bare
assertion), shown at a glance.

> Rename note (#179): this skill took the `report` name. The previous `report`
> skill (manager worker-activity deck via `cos-console` `deck.generate`) is now
> [[manager-recap]] — same behaviour, new name.

## The four requirements it satisfies

1. **Sidebar, multi-page, one page at a time** — `<aside>` nav; each `.page` is
   `x-show="route===..."`; exactly one is visible. Keyboard: `[`/`]` or `j`/`k`
   between pages, `p` to print.
2. **Extracted source (un-busy HTML)** — four files: `review.template.html`
   (structure) · `review.css` (style) · `review.js` (behaviour, an Alpine
   component) · the JSON data model. The build inlines them; nothing is
   hand-concatenated.
3. **Convertible to PDF** — a `@media print` block un-hides every page (one per
   printed page), hides the sidebar/controls, and switches to a light theme.
   Hit **Print / PDF** in-browser, or run `to_pdf.sh` for a deterministic
   headless PDF.
4. **Embedded JSON, easy review/extraction** — the whole model lives in one
   `<script id="report-data" type="application/json">` blob; the **Data** page
   pretty-prints it with **Copy** / **Download** buttons; every page derives from
   that one blob (add a view = add a page, no data plumbing).

Plus the boss's hard rule — **single file, single everything**: Alpine is
vendored and **inlined** (no CDN), so the deliverable works with no network and
survives being emailed.

## Layout

```
skills/report/
  SKILL.md
  assets/
    review.template.html   # shell: <aside> sidebar + <main> pages, Alpine directives
    review.css             # screen styles + @media print (colours match the deck)
    review.js              # Alpine component `reportApp`: routing, keyboard nav, export
    alpine.min.js          # vendored Alpine 3.14.1 — inlined for offline use
  scripts/
    render_review.py       # data model -> ONE self-contained .html (inlines assets; gated)
    to_pdf.sh              # headless-chromium print-to-pdf wrapper
    gen_ai_harness_report.py  # builds the ai-harness report model from real sources (#179 proving case)
  fixtures/
    ai-harness.json        # the generated ai-harness report data model
  verification/
    review-*.png, ai-harness-report.pdf
```

## Invocation

```bash
# 1. build/obtain a data model (see schema below), then render it:
python3 skills/report/scripts/render_review.py --data model.json -o out.html
#    --linked keeps assets as separate files for fast edit-reload (dev mode).

# 2. deterministic PDF (the in-browser Print button is the zero-dep path):
skills/report/scripts/to_pdf.sh out.html out.pdf
```

`render_review.py` **gates the build** (the deck_src "a single-file page fails
silently" lesson): the JSON must parse, every template placeholder must be
filled, all seven page anchors must be present, and Alpine must actually be
inlined — otherwise it aborts loudly rather than ship a blank page.

## Data model (embedded JSON)

```jsonc
{
  "meta": { "project": "...", "title": "...", "slug": "...", "window": "...",
            "generated_at": "...",
            "freshness": { "level": "fresh|aging|stale", "note": "..." },  // banner
            "sources": [ { "name": "...", "detail": "<the command used>" } ] },
  "kpis":  [ { "label": "...", "value": 10, "na": false } ],   // na:true => "n/a", never a fake 0
  "tasks": [ {
     "id": "...", "title": "...", "status": "done|in-progress|blocked|todo",
     "progress": 0.7, "owner": "...", "note": "...",
     "todos":     [ { "text": "...", "done": true } ],
     "resources": [ { "kind": "ticket|commit|url|file|log|env|board|repo", "label": "...", "href": "..." } ],
     "evidence":  [ { "kind": "image|render|command|diff|metric", "verified": true, "caption": "...",
                      "img": "data:image/png;base64,…",   // image (embedded => self-contained)
                      "srcdoc": "<html>…</html>",           // render (sandboxed iframe preview)
                      "cmd": "...", "out": "...", "exit": 0, // command
                      "patch": "...",                        // diff
                      "value": "…", "unit": "…",             // metric
                      "href": "...", "href_label": "..." } ] // optional "open source" link
  } ],
  "resources": [ /* report-wide typed links, merged with per-task resources on the Resources page */ ],
  "timeline":  [ { "ts": "...", "event": "...", "actor": "...", "note": "..." } ],
  "facts_guidance": "<html snippet: when to record a fact>",
  "facts": [ { "statement": "...", "why": "...", "recorded_because": "...", "date": "...",
               "source": { "label": "...", "href": "..." }, "memory_ref": "<file>.md" } ]
}
```

Rows/cards **describe and link — they do not restate figures** that live in a
source document (the `Trackers_Index` principle: a stale number copied here
misleads *before* the reader reaches the doc that would correct it). Progress and
Validation sort **worst-first** (blocked → in-progress → todo → done).

## Anti-fabrication (non-negotiable)

- A KPI with no data is `na: true` → renders **n/a**, never a fabricated `0`.
- Evidence is tagged **verified** only when a resolvable pointer is attached;
  otherwise **claimed**. A task with **zero verified evidence** renders a loud
  **UNVERIFIED — no resolvable proof attached** banner.
- If a source was unavailable or a limitation was hit, say so **on the page**
  (e.g. as a fact/caveat) — the ai-harness report records the "worker sessions
  can't set board Status because their gh token lacks the `project` scope" fact
  exactly this way, WITH a "please verify" that caught (and corrected) an earlier
  misdiagnosis before it propagated.

## Facts / Memory page — and when to record a fact

The **Facts / Memory** page carries **durable facts, conventions, and decisions
that outlive a single session** — the generatable version of a hand-built
tracker's yellow "convention" box. Each entry: the **statement**, **why** it
exists, the **date**, a **source** link, and (where relevant) **recorded because
X**.

**When a session should record a fact** (author into `facts[]`, and prefer a
real memory file):

- a **convention that keeps getting undone** (write it down so the next agent
  doesn't re-break it),
- a **decision whose rationale isn't in the code** (the "why", not just the
  "what"),
- a **gotcha rediscovered more than once** (e.g. the flaky run-marker race).

Let it pass when it's already captured by the repo (code structure, git history,
CLAUDE.md) or only matters to the current conversation.

This page **surfaces** the existing memory model — the one-fact files under
`~/.claude/projects/*/memory/` plus the `MEMORY.md` index — it does **not**
become a second source of truth. Set `memory_ref` to the memory file and prefer
linking over restating.

## PDF — two paths, both kept

1. **Zero-dep, in-browser:** the `@media print` block un-hides every page, one
   per printed page, drops the sidebar/controls, switches to a light theme →
   **Print / PDF** → Save as PDF. Works everywhere, offline.
2. **Deterministic, headless:** `to_pdf.sh <in.html> <out.pdf>` drives
   `chromium --headless=new --print-to-pdf`. **Do not** add
   `--virtual-time-budget` — under `--print-to-pdf` it snapshots before Alpine
   finishes and prints empty page bodies (verified #179; the deck_src
   "headless lies" trap).

## Reuse / provenance (#179)

- The single-file **inliner pattern** (token substitution → one self-contained
  HTML) and the **build-gate** discipline are adapted from
  `divorcio/common/deck_src/build.py` (studied read-only; not imported — that
  builder is coupled to its deck's variants/locale/fallback machinery).
- `to_pdf.sh` mirrors the headless-Chromium posture of `deck_src/make_pdf.py`
  and narrate/joint-browser.
- Colour tokens and the `.tile/.card/.pill` vocabulary match
  `cos-console/presentation-poc/deck` so the review page and the exec recap read
  as one system.
- **Deviation from the spec:** Alpine.js is used (spec assumption A1) and
  vendored+inlined; the vanilla-JS fallback was not needed since inlining keeps
  the file self-contained and offline.
