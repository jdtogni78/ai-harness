# Manager registry + routing + daily-sweep

Status: implemented (#145). Extends the [[manage]] and [[meta-manage]] skills
so managers **register** their goals/responsibilities, **route** out-of-domain
work to the right manager, **report** progress via a once-daily meta sweep, and
so the meta-manager holds a **project→manager map** and decides when to **spin**
a new manager.

Built to three shaping decisions (do not relitigate — see #145):

1. **Hybrid manager model.** Two manager kinds coexist:
   - **domain** — a STANDING manager that owns a domain (e.g. a skill-manager
     owning `skills`, an infra-manager owning `infra`). Long-lived; work in its
     domain routes to it.
   - **project** — an AD-HOC, time-boxed manager for one initiative, tied to a
     GitHub board or a repo. Retires when the initiative ends.

   The registry carries both, discriminated by `kind`.
2. **Daily report = META PULL-SWEEP (scheduled).** The meta-manager runs a
   once-daily sweep that pulls a one-line progress update from every live
   registered manager into a consolidated progress log. This is NOT
   per-manager self-timers — the meta side owns the cadence.
3. **Project list = GitHub boards + a supplement.** The GitHub Project boards
   ARE the canonical projects. A small committed supplement file
   (`skills/meta-manage/system-initiatives.jsonl`) adds the non-board *system
   initiatives* (the naming system, the answers feed, this registry itself).

## Where things live

| Thing | Path |
|---|---|
| Registry ledger | `~/.ai-harness/manager/registry.jsonl` (sibling of `ordinals.jsonl` + the per-manager logs) |
| Progress log | `~/.ai-harness/manager/progress.jsonl` (consolidated daily sweep output) |
| System-initiative supplement | `skills/meta-manage/system-initiatives.jsonl` (committed) |
| Helper | `skills/manage/scripts/registry.sh` (vendored as a symlink under `~/.claude/skills/…`) |

The registry sits in the SAME state dir and follows the SAME conventions as
`workers.sh` / `answers.sh`: append-only JSONL folded on read, a mkdir-based
lock (macOS has no `flock(1)`), records are never rewritten or recycled, and
`$MANAGER_STATE_DIR` overrides the dir for tests.

## Three hardening decisions (MGR-11 review, #145)

Three gaps were closed before managers depend on the registry — each had a
direct precedent in this repo:

1. **Responsibilities are a controlled vocabulary, normalized on write AND
   read.** Free-text `responsibilities` would silently fail to match
   (`skill` vs `Skills` vs `skills `), and `lookup` returning nothing reads as
   "no owner" — which would make the meta-manager spin a DUPLICATE manager for a
   domain that already has one. Same shape as MGR-12's `_ordinal_band` literal
   bug (#137/#139). Fix: a small repo-owned vocabulary + alias map in
   `_canon_resp` (skills, infra, trading, finance, legal, deck, sessions,
   harness, coordination), applied identically on `register` and `lookup`
   (lowercase, trim, plural-tolerant, aliased). An out-of-vocab token is
   ACCEPTED but WARNED loudly — unknown-but-visible is fine; unknown-and-silent
   is the failure we refuse.
2. **Domain responsibilities are EXCLUSIVE; project managers may overlap.**
   Nothing else stops two managers claiming one responsibility — the
   duplicate-ordinal problem again (#129). Decision: `register --kind domain`
   REFUSES a responsibility already held by another active domain manager
   (`--force` overrides); `--kind project` overlaps freely. A domain
   double-claim is a detectable `audit` fault regardless of liveness.
3. **The daily sweep distinguishes `reported` / `unreachable-busy` /
   `no-response`.** A busy manager returns `409 session_not_active`, so the
   pull would miss exactly the managers doing the most work — and a missing
   line reads as "no progress" instead of "couldn't reach". `set-report
   --status` records the non-response explicitly (no `--note` required for a
   non-`reported` status), so an unreachable manager NEVER renders as silence.
   Retry policy: skip-and-retry next sweep; never drop silently.

**set-report authority.** `set-report` writes two places from ONE note, so they
never disagree: `registry.last_note`/`last_report_status` is the CURRENT-STATE
snapshot (latest only, folded); `progress.jsonl` is the append-only HISTORY the
boss reads per sweep.

## Registry schema

Append-only JSONL. Three event types, folded on read into one record per
manager (keyed by `cse_id`):

- **register** — `{event:"register", cse_id, mgr_ord, kind:"domain"|"project",
  name, goals, responsibilities:[...], board, project, status:"active",
  registered_at}`. Re-registering the same `cse_id` updates its fields (a new
  append, never a rewrite); `registered_at` is preserved from the first
  register.
- **report** — `{event:"report", cse_id, note, ts}`. Sets `last_report_at` +
  `last_note` on the folded record. Emitted by `set-report` (which also appends
  to `progress.jsonl`).
- **retire** — `{event:"retire", cse_id, reason, ts}`. Sets `status:"retired"`.
  Emitted by `retire` and by `audit --fix` when a holder's session is archived.

Folded record shape:

```json
{
  "cse_id": "cse_…", "mgr_ord": 12, "kind": "domain",
  "name": "skill-manager", "goals": "own the skills domain",
  "responsibilities": ["skills"], "board": null, "project": null,
  "status": "active", "registered_at": "2026-07-27T…Z",
  "last_report_at": "2026-07-27T…Z", "last_note": "shipped #144"
}
```

## Helper interface — `registry.sh`

```
registry.sh register --kind domain|project --name "<n>" --goals "<…>"
                     --responsibilities "skills,infra"
                     [--board N] [--project "<repo>"]
                     [--cse <id>] [--mgr-ord N]     # overrides for bootstrap
                     [--force]                       # override a domain conflict
registry.sh list     [--all] [--json]               # active only by default
registry.sh lookup   --responsibility skills        # → owning manager's cse_id (exit 4 if none)
registry.sh set-report --cse <id> --note "<progress>"
                       [--status reported|unreachable-busy|no-response]
registry.sh retire   --cse <id> [--reason "<…>"]
registry.sh audit    [--fix]                        # reconcile vs live sessions
registry.sh projects [--json]                       # boards+supplement, owner-annotated
registry.sh canon    "<csv>"                        # debug: normalized responsibilities
registry.sh path / progress-path                    # print file paths
```

- `register` without `--cse`/`--mgr-ord` resolves the CALLER's own `cse_id`
  (same JWT path as `workers.sh`) and its allocated ordinal from
  `ordinals.jsonl`. The overrides exist so the meta-manager can bootstrap /
  register OTHER managers from the live list.
- `lookup` returns the `cse_id` of the single **active** manager owning that
  responsibility (exit non-zero if none — the routing rule's "no owner" branch).
  Liveness is checked by the CALLER before relaying (see routing flow).
- `audit` reconciles the ledger against the LIVE session list in BOTH
  directions (see below). Takes the live list as input (injectable via
  `REGISTRY_LIVE_JSON=<file>` for tests) so it can't miss a claimant.

## The four flows

### 1. register — first-turn, piggybacks on self-title

A manager's first-turn protocol already does `workers.sh retitle "<task>"`
(which allocates its `MGR-N`). Registration is the very next step: the manager
calls `registry.sh register …` declaring its `kind`, `goals`,
`responsibilities`, and `board`/`project`. A **domain** manager registers the
domains it owns; a **project** manager registers its board/repo. Codified in
`manage/SKILL.md`'s first-turn protocol.

### 2. route-offload — consult the registry before out-of-domain work

When a manager gets a request OUTSIDE its own responsibilities, it MUST consult
the registry before doing the work:

```bash
owner="$(registry.sh lookup --responsibility <x>)"
```

- **owner is a LIVE manager** → RELAY to it via `send-to-session` (don't do it
  yourself). Canonical example: a manager asked to "add a skill" relays to the
  skill-manager.
- **no owner (lookup non-zero) OR the owner's session is not live** → ESCALATE
  to the meta-manager (`MGR-11`), which decides whether to spin one.

A manager may relay to an existing owner directly, but MUST NOT spin a sibling
manager itself — spinning a new manager is the meta-manager's call.

### 3. daily-sweep — meta pull-sweep into the progress log

Once-daily, meta-driven. The meta-manager:

1. DISCOVERs the live registered managers (`registry.sh list --json`, joined
   with `sessions --json` for liveness).
2. Batches ONE brief to every live manager via `send-to-session` (same shape as
   the existing pending-check sweep): *"one-line progress update since your last
   report."*
3. Records each reply with `registry.sh set-report --cse <id> --note "<…>"`,
   which stamps `last_report_at` on the registry record AND appends a line to
   `~/.ai-harness/manager/progress.jsonl` — so the boss reads ONE consolidated
   place.

**Scheduling.** The once-daily trigger is a cron that nudges the meta-manager
session to run its sweep (`CronCreate` / the schedule skill). The sweep command
is BUILT and documented; the live recurring cron is **boss-gated** (a standing
scheduled job is a recurring side-effect) — see `meta-manage/SKILL.md` for the
enable/disable snippet.

### 4. spin — meta project-list + spin decision

`registry.sh projects` joins the GitHub boards (canonical projects) + the
system-initiative supplement, annotates each with its owning manager from the
registry, and FLAGS any project/initiative with NO live manager as a
**spin candidate**. On an unmatched request or an unowned project, the
meta-manager spins a manager (`new-session`) and registers it. This includes
standing up the STANDING domain managers — at least a **skill-manager** owning
`skills`, so future skill builds (joint-browser #144, the answers feed) route
to it instead of being done ad-hoc.

## Audit — both directions

`registry.sh audit` mirrors `workers.sh mgr-audit`'s discipline (MGR-12, #129):

- **registry → live**: an active entry whose `cse_id` is NOT in the live
  session list is a stale holder → problem ("retire it"). `--fix` retires it.
- **live → registry**: a live session with a `[MGR-N]` title that holds NO
  active registry record → problem ("register it").
- An empty live list is refused as an audit input (a transient API failure
  would otherwise flag every entry) — same guard as `mgr-audit`.

Exit 0 when consistent, exit 1 when inconsistent. Verified in BOTH directions in
the test suite (clean ledger → 0; a planted stale/unregistered entry → 1) —
that both-directions discipline is what makes the checker trustworthy.

## How this touches each skill

| Skill | Change |
|---|---|
| `manage/SKILL.md` | first-turn "Register yourself" step (flow 1); the route-offload rule (flow 2) |
| `meta-manage/SKILL.md` | the daily-sweep operation + progress-log location + schedule enable/disable (flow 3); the projects list + spin decision incl. standing the skill-manager (flow 4) |
| `skills/manage/scripts/registry.sh` | new helper |
| `skills/meta-manage/system-initiatives.jsonl` | new committed supplement |
| `tests/test_registry_sh.py` | helper unit tests (no network) |
</content>
</invoke>
