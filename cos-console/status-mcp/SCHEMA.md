# StatusReport schema — the cos-console data contract

**Status: FROZEN v1.0** (owned by W0 / data-plane). Voice workers (w1/w2/w3)
converge on this shape. Machine-readable copy: [`status_report.schema.json`](./status_report.schema.json)
(JSON Schema draft-07). This doc is the human-readable companion.

Every voice POC calls exactly one tool — `get_project_status(project) -> StatusReport`
— and gets this object back. Stub against it until you wire the real MCP; the
shape will not change under you without a note in the "Changelog" below.

## Design rules (why it looks like this)

1. **Additive over the PROJECT.md v0 stub.** Every v0 field name is preserved
   (`tickets.todo`, `tests.count`, `deploy.last_deployed_at`, `visual_review.done`,
   `decisions[].when`, `open_questions`, …). New fields are all optional, so a
   voice worker's v0-shaped stub still validates.
2. **Never fabricate.** When a signal isn't reachable the numeric fields are
   `null` (NOT `0`) and the section is flagged in `availability`. `0` means "we
   looked and there are zero"; `null` means "we couldn't look". This distinction
   is what lets the voice layer say *"I don't have test data for dstrader"*
   instead of confidently reading a fake `0 passing`.
3. **`availability` is the honesty layer.** Read it before voicing any number.

## Top-level fields

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.0"`. |
| `project` | string | Registry key, e.g. `"dstrader"`. |
| `generated_at` | ISO8601 (UTC) | When the report was assembled. |
| `availability` | object | Per-section provenance: `live` \| `partial` \| `unavailable`. Keys: `tickets`, `tests`, `deploy`, `visual_review`, `decisions`. |
| `tickets` | object | See below. |
| `tests` | object | See below. |
| `deploy` | object | See below. |
| `visual_review` | object | See below. |
| `decisions` | array | See below. |
| `open_questions` | string[] | Free-text prompts for the operator ("no tests found — run the suite?"). |
| `warnings` | string[] | Non-fatal collection issues (auth missing, path absent). |

### `tickets`
GitHub Projects board status for the project's repo(s).
```
{ "total": 12, "todo": 3, "in_progress": 1, "done": 8, "blocked": 0,
  "items": [ { "id": "64", "title": "...", "state": "Todo",
              "url": "https://github.com/...", "repo": "jdtogni78/dstrader" } ],
  "source": "gh project item-list 1 (repo=jdtogni78/dstrader)" }
```
- `state` is the **board Status** (`Todo`/`In Progress`/`Done`/`Blocked`), not
  the issue open/closed state — that's the signal the chief-of-staff cares about.
- Board **draft notes** (no underlying issue) can't be attributed to a repo;
  they're excluded from counts and noted in `warnings`.

### `tests`
```
{ "available": true, "count": 42, "passing": 41, "failing": 1, "skipped": 0,
  "coverage_pct": 73.4, "last_run": "2026-07-17T...Z",
  "source": "target/surefire-reports + target/site/jacoco/jacoco.csv" }
```
- `available: false` ⇒ every numeric field is `null`. Do not read as `0`.
- Parses Maven **surefire** XML (`target/surefire-reports/*.xml`) for
  count/pass/fail/skip + `last_run` (newest report mtime), and **JaCoCo**
  (`jacoco.csv` or `jacoco.xml`) for `coverage_pct`. Missing → nulls.

### `deploy`
```
{ "last_deployed_at": "2026-07-17T21:28:54Z", "env": "dstrader-docker",
  "commit": "2f4bf29b53", "status": "ok", "duration_s": 2,
  "target": "dstrader-docker", "source": ".../deploy_logs/INDEX.md" }
```
- Read-only parse of the deploy-log `INDEX.md` table (newest row for the
  project's deploy target). `status` normalizes to `ok`/`failed`/`unknown`.
- **Never deploys.** Reporting layer only.

### `visual_review`
```
{ "done": true,
  "artifacts": [ { "name": "investment-strategy.demo.yaml", "kind": "demo_script",
                   "path": ".../demos/...", "when": "2026-06-04T...Z" } ],
  "source": ".../demos" }
```
- `kind` ∈ `video` \| `demo_script` \| `explainer` \| `screenshot` \| `other`.
- `done` is true iff at least one video/demo/explainer artifact exists.

### `decisions`
```
[ { "when": "2026-07-18T...Z", "summary": "Merge branch '66-eod-...'",
    "source": "merge-commit", "ref": "7e6fc0e" } ]
```
- Mined from merge commits (PR titles) + `close-work`/handoff briefs.
- `source` ∈ `merge-commit` \| `close-work` \| `handoff`.

## Changelog
- **v1.0** (2026-07-18) — Frozen. Superset of PROJECT.md v0 stub. Added
  `schema_version`, `availability`, `warnings`, `tickets.total`/`items[].repo`,
  `tests.available`/`skipped`/`source`, `deploy.duration_s`/`target`/`source`,
  richer `visual_review.artifacts[]` objects, `decisions[].ref`.
