---
name: validate
description: >-
  Manager-invoked, milestone-driven project VALIDATOR — the outcome counterpart
  to /report (activity). /validate <project> reads that project's MAJOR
  end-to-end GOALS from a per-project, manager-owned milestones file and, for
  EACH goal, tries to PROVE it works with REAL resolvable evidence (a file that
  exists, a pattern found, a command that ran green). It embeds the #110
  validator stance — assume nothing works until proven, demand pasted proof,
  REVIEW rendered outputs — and reuses the /report skill's verified-vs-claimed
  classifier: VERIFIED-WORKING requires a resolvable evidence pointer, never a
  bare claim. Output: milestone-status TABLES (worst-first) + an HTML deck
  (REUSING cos-console/presentation-poc/deck.generate) + a markdown recap. Use
  when a manager says "validate <project>", "prove our milestones work", "are
  the major goals actually working", "milestone/outcome status", "which goals
  are proven vs unverified", or "generate a milestone deck".
---

# /validate — manager-invoked milestone validator

A **validator** the [[manage]]r invokes for **its own project**. Where
[[report]] recaps *activity* ("what did my workers do?"), `/validate` tracks
*outcomes*: **do this project's MAJOR END-TO-END GOALS actually work, and can we
prove it?** It is **read-only w.r.t. every validated system** — it never mutates
a project to make a goal pass; its only writes are the deck it renders.

Each manager owns its own project's milestones file, so the validator is scoped
to whatever project the manager runs it for.

## Invocation

```bash
python3 skills/validate/scripts/validate.py <project> \
    [--milestones-dir DIR] [--outdir DIR] [--slug NAME] [--json]
```

- **`<project>`** — reads `milestones/<project>.yaml`. Seeded projects:
  `cos-console` (known goals), `dstrader` (operator-CONFIRMED), `familyfund`
  (**DRAFT**, unconfirmed).
- Prints a **markdown milestone table** (worst-first) to stdout and writes the
  deck under `--outdir` (default `cos-console/presentation-poc/out/`):
  `<slug>.html`, `<slug>.narration.json`, `<slug>.demo.yaml` (slug default
  `<project>-milestones`).

## The stance (why this exists)

Inherited from the #110 validator audit and the [[report]] honesty bar:

- **Assume nothing works until proven.** A milestones file's declared status is
  a **label, never an upgrade** — evidence always wins. If the file claims a
  stronger status than the evidence supports, that's surfaced as a
  **discrepancy**, not silently trusted.
- **VERIFIED-WORKING requires a resolvable evidence pointer** the validator
  checked itself. This reuses the [[report]] classifier
  (`report.resolve_pointer`) as the canonical "does this pointer resolve" test —
  the same green/amber bar `/report` uses for tests.
- **Missing proof is a FINDING, never a pass** and never a fabricated green: a
  goal with no resolvable evidence is UNVERIFIED / NOT-YET / PARTIAL / FAILED,
  shown at its honest verdict.
- **REVIEW rendered outputs** — after generating the deck, actually open/screenshot
  it (see below); don't trust the exit code.

## Verdicts (worst-first in every table)

| verdict | means |
|---|---|
| **FAILED** | an evidence check ran and did **not** confirm the goal |
| **UNVERIFIED** | evidence expected but **not resolvable here** (system likely unreachable) — a finding, never a 0 |
| **NOT-YET** | operator-declared not built yet (`expect: not-yet`), no evidence resolvable |
| **PARTIAL** | some evidence resolved but something is still `pending` — cannot be green |
| **VERIFIED-WORKING** | **every** declared check resolved and nothing pending — the only verdict that requires proof; the deck cites the pointer |

## Milestones file — per project, manager-owned, editable

`milestones/<project>.yaml`. Each goal: `id`, `goal` (one line), `prove` (how
evidence is gathered), and an `evidence:` list of checks. Full schema is
documented in-file at the top of `milestones/cos-console.yaml`. Check kinds
(each resolves to **PASS / FAIL / MISSING**):

```yaml
evidence:
  - file: path/that/must/exist              # PASS if it exists on disk
  - grep: {pattern: "<re>", path: <path>}   # PASS if pattern found in the file
  - command: {run: "<shell>", expect_rc: 0, expect_match: "<re>"}  # read-only
```

- `pending:` — a list of aspects **not yet provable**; any entry caps the goal
  at **PARTIAL** (it can never be VERIFIED-WORKING while something is pending).
- `expect:` — the operator's declared status for goals with no automatable proof
  (`not-yet` / `partial` / `failed`). A **label**, never an upgrade; a mismatch
  vs the evidence is flagged as a discrepancy.
- `draft: true` — badges the whole file **UNCONFIRMED** in the tables and deck
  (familyfund is seeded this way — its goals are the author's guess, not the
  operator's contract).

Paths resolve relative to the ai-harness repo root (also `~` and absolute), so
`dstrader`/`familyfund` evidence points where those systems would write it; run
from a host where they're live and it resolves, from here it's honestly
UNVERIFIED.

## Presentation — HTML deck reusing deck.generate (same as /report)

The deck is rendered by **importing** `cos-console/presentation-poc/deck.generate`
(`write_generic_deck`) — the renderer is **reused, not duplicated**, through the
same template chrome (`.tile`/`.card`/`.clean`/`.nodata-panel`, hash-router nav,
`narration.json` + narrate-demo `.demo.yaml` companions) as the report and
StatusReport decks. Slide order: **summary → milestone table (worst-first) →
evidence detail → gaps/open questions**. A narrated MP4 is optional — the
emitted `<slug>.demo.yaml` is a ready [[narrate-demo]] script.

## How to run it (manager flow)

1. Run the script for your project. Read the markdown table it prints — worst
   goals first, each green row citing its evidence pointer.
2. **Review the rendered deck** (the #110 rigor — look, don't trust the exit
   code). Screenshot with headless Chrome:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
     --disable-gpu --hide-scrollbars --window-size=1440,900 \
     --virtual-time-budget=1800 --screenshot=out.png "file://…/<slug>.html#1"
   ```
   (`#1` = the milestone table, `#2` = evidence detail.)
3. Relay the table + the honest gaps to the boss. NOT-YET/UNVERIFIED goals are
   the real state — present them as such, never dressed up as working.

## Rules

- **Read-only** w.r.t. every validated system; never mutate a project to make a
  goal pass. Only writes are the deck files.
- **Never render a goal working without proof.** VERIFIED-WORKING requires a
  resolvable evidence pointer; everything else is a finding at its honest verdict.
- **Never fabricate.** Missing evidence is UNVERIFIED / "no evidence checks",
  never a 0 or a green.
- **Evidence beats the file.** A milestones file's `expect:` never upgrades a
  verdict; over-claims are flagged as discrepancies.
- **Reuse the renderer** (`deck.generate`) and the **classifier**
  (`report.resolve_pointer`) — do not copy either.
- Each manager maintains its **own** `milestones/<project>.yaml`.
