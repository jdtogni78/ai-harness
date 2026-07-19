# FINDINGS — status-mcp (W0, data plane)

What real data was reachable on this host vs what's stubbed/nulled, the gaps,
and what the voice layer can rely on **today**. Measured against `dstrader`
(first target) and `familyfund` (second, to prove `--project` generalizes).
Both produce reports that pass `status_report.schema.json`.

## Reachability scorecard (dstrader, 2026-07-18)

| Section | Availability | Real? | Source proven to work |
|---|---|---|---|
| **tickets** | `live` | ✅ real | `gh project item-list 1` → 41 issues attributed to `jdtogni78/dstrader` (15 Todo / 0 In-Progress / 26 Done / 0 Blocked). |
| **deploy** | `live` | ✅ real | `~/dev/dstrader-docker/local/deploy_logs/INDEX.md` → last = `2026-07-17T21:28:54Z`, target `dstrader-docker`, `ok`, commit `2f4bf29b53`, 2s. |
| **visual_review** | `live` | ✅ real | `~/dev/dstrader/demos` → 2 `.demo.yaml` + 2 `*explainer*.html`. `done=true`. **No rendered `.mp4` yet** (flagged in warnings). |
| **decisions** | `live` | ✅ real | `git log --merges` in `~/dev/dstrader` → 12 merge commits with PR titles + timestamps. |
| **tests** | `unavailable` | ⚠️ nulled (honest) | No `target/surefire-reports/*.xml` and no JaCoCo present — suite hasn't been run. All numeric fields `null` (NOT 0). Verified the parser works via a synthetic fixture (10 tests / 1 fail / 2 skip / 85% coverage → correct). |

## What the voice layer can rely on TODAY

- **A schema-valid StatusReport for any registered project, always.** No
  collector can crash the report; a dead signal degrades to
  `availability.<section> = "unavailable"` + a `warnings[]` entry.
- **4 of 5 sections are live real data for dstrader** (tickets, deploy,
  visual_review, decisions). This is enough to answer the core chief-of-staff
  questions: *"What's in flight? Did it deploy? When? Was there a visual review?
  What decisions were made?"*
- **The `availability` map is the honesty contract.** Voice workers MUST read it
  before asserting a number. `tests` returning `null` means *"I haven't run /
  can't find the suite"* — the CoS should SAY that, not read a fake "0 passing".
- **`open_questions` is pre-chewed for voice.** The report already derives
  operator-facing prompts ("No test artifacts were found — has the suite been
  run recently?"), so the brain has something to volunteer without extra logic.

## Gaps / caveats (what's NOT solved yet)

1. **Tests are dark until the suite runs.** dstrader is a Maven project but has
   no committed surefire/coverage artifacts. Parser is proven against a fixture;
   it will light up the moment someone runs `mvn test`. *No fabrication* is the
   deliberate choice. (Ticket #63 tracks a TWS mock to enable CI tests — until
   then this stays `unavailable` on dev-mini.)
2. **Board draft notes can't be attributed to a repo.** 9 draft notes on board 1
   have no underlying issue/repo link, so they're excluded from counts and noted
   in `warnings`. If those matter, they'd need a manual project→note mapping.
3. **Tickets = board Status, not issue open/closed.** `state` is the board
   column (Todo/In Progress/Done/Blocked) — the signal a CoS cares about — which
   can lag the raw issue state. `Blocked` relies on a board column of that name;
   label-based `blocked` is not yet merged in.
4. **`decisions` currently mines merge commits only.** `close-work`/handoff-brief
   mining is scaffolded (config `brief_dirs`) but not yet extracting — the
   handoff JSONs aren't reliably project-keyed. Merge commits alone give solid,
   dated, real decisions.
5. **Deploy log is host-local.** `INDEX.md` lives on whichever host ran the
   deploy script (prod deploys are MacBook-only per the operator rule). On a host
   without it, deploy degrades to `unavailable` cleanly. Override the path via
   `STATUS_MCP_DEPLOY_INDEX`.
6. **`gh` project scope required.** Without `project` scope, tickets go
   `unavailable` (the rest still works). Documented in README + `.env.example`.

## Setup effort / cost

- **Setup:** ~zero for the probe (pure stdlib). Server needs `pip install mcp`.
  `gh` was already authed with the `project` scope on this host.
- **Latency:** report assembly is dominated by the single `gh project item-list`
  call (~1–2s) + `git log` (~instant). Deploy/tests/visual are local file reads.
  Rough end-to-end: **~2s** warm.
- **Cost:** $0 — no LLM calls, no paid APIs. All local + GitHub API (free).

## One-line recommendation

**Ship this as the shared backend as-is** — 4/5 sections are live for dstrader,
the schema is frozen + validated, and the `availability`/`null` discipline means
the voice layer can trust every field it reads. Wire `tests` by running the
suite (or a CI job) to emit surefire/JaCoCo; nothing else is blocking.
