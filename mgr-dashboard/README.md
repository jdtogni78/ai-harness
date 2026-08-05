# mgr-dashboard — sessions grouped by manager (read-only)

Static HTML dashboard that groups live Claude Code sessions by `[MGR-N]` and
turns each row into a `claude://resume?session=<uuid>` deep link. Sidesteps the
app's private local group store entirely (issue #158). **Read-only**: one API
`GET`, plain transcript file reads, zero writes to any app store or the API.

Top-level tool dir (mirrors `narrate/`). A thin loadable `SKILL.md` wrapper that
references this tool is a **follow-up**, not part of #158.

## Run

```bash
cd ~/dev/ai-harness
python3 mgr-dashboard/mgr_dashboard.py -o /tmp/mgr-dashboard.html --open
```

`-o` output path (default `mgr-dashboard.html`) · `--open` opens it · `--projects-root` override transcript dir.

## How it maps `cse_` → deep-link uuid

The deep link needs the **CLI transcript uuid**, not the `cse_` id — and the
`cse_` id is *not* stored in the transcript, nor returned by the sessions API.
So we match on time: a bridge relays turns in lockstep with its local `claude`
process, so a session's API `last_event_at` ≈ its transcript's last timestamp.
Nearest-timestamp greedy assignment (each transcript used once), scoped per repo.
Confidence = |Δ|: **high ≤5s · medium ≤60s · low ≤300s**; beyond 300s the row is
shown **unmapped** rather than linked to the wrong session. Off-host (mini) and
long-idle sessions land unmapped by design — we don't guess.

## Title forms understood

`[MGR-20] …` (manager) · `[AH.m5][MGR-12] …` (legacy nick-leading) ·
`[MGR17-W9] …` / `[MGR13][W5] …` (worker, combined + split) · body-text
`… MGR7-W21 …` · `[NOMRG]` / `[INBOX]` / nick-only → **Unmanaged** (listed last).

## Deep-link semantics (focus vs import)

`claude://resume?session=<uuid>` → app handler `Fr.Resume → importCliSession`.
**Open question the ticket requires us to settle with exactly ONE manual fire:**
does it *focus* the already-open bridge session or *import a duplicate*?

Procedure (fire ONE, never bulk):
```bash
python3 -m remote_control sessions --json > /tmp/before.json   # snapshot
open "claude://resume?session=<a-high-confidence-uuid>"          # single fire
python3 -m remote_control sessions --json > /tmp/after.json    # snapshot
diff <(jq -r '.[].id' /tmp/before.json|sort) <(jq -r '.[].id' /tmp/after.json|sort)
```
A new `cse_` in `after` ⇒ **imports a duplicate**; no new id ⇒ **no new session**.

**Finding (one boss-approved fire against a live #158 session, 2026-07-31):**
**no duplicate server-side session is created** — the session list stayed 26→26
with no new `cse_` across ~20 polls of `GET /v1/code/sessions`. That is exactly
the acceptance criterion (the deep link does not spawn a duplicate cloud
session). Scope of the claim: only the *server-side* no-duplicate behavior was
verified; the GUI focus-vs-read-only-import *view* was **not** visually
observed, so we don't claim "focuses the window."

## Files

`mgr_dashboard.py` — parser + mapper + renderer + CLI ·
`../tests/test_mgr_dashboard.py` — `python3 -m unittest tests.test_mgr_dashboard` (20 tests).
