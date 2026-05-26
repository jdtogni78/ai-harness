# Usage-limit auto-resume monitor v2 (API-based)

Status: planned 2026-05-21. Supersedes the JSONL-scanning engine (commit `9b41b63`).

## Why the v1 (JSONL) engine is broken

v1 scanned `~/.claude/projects/**/*.jsonl` for `429` entries and ran
`claude --resume <uuid> -p continue`. Proven wrong on 2026-05-21:

- The local JSONL session id (a uuid like `897c7901…`) is a **different
  conversation** from the cloud/bridge session (`cse_…`) shown in the app.
  Resuming the uuid never touches the session the user sees.
- For live bridged (`mini:…`) sessions the usage-limit pause is
  bridge/cloud-side state and often isn't written to the local JSONL as a
  `429` at all, so the scanner can't even detect it.

## v2 architecture: the code-sessions API

Auth: token from keychain each cycle —
`security find-generic-password -s "Claude Code-credentials" -w` →
`.claudeAiOauth.accessToken` (never logged). Headers:
`Authorization: Bearer <t>`, `anthropic-version: 2023-06-01`,
`anthropic-beta: oauth-2025-04-20`. On 401: skip cycle (running CLIs refresh
the keychain credential before its ~hours-out expiry).

### Detect — `GET /v1/code/sessions?limit=100`
Paused-on-limit when ALL hold:
- `status == "active"`
- `worker_status == "idle"` (skip `running` / `requires_action`)
- `external_metadata.post_turn_summary.status_category == "failed"`
- `status_detail` matches `usage limit` OR `session limit` (regex)
- exclude other `failed` details (e.g. `Path "..."` errors)
- skip the monitor's own session id
Active sessions sort to the top, so one page suffices.

### Resume — `POST /v1/code/sessions/{id}/events`
```json
{"events":[{"event_type":"user","source":"client",
  "payload":{"type":"user","message":{"role":"user",
    "content":[{"type":"text","text":"continue"}]}}}]}
```
200 ⇒ accepted. Verify by re-GETting: `worker_status` left `idle` /
`post_turn_summary` cleared ⇒ took; still `failed` ⇒ re-limited, back off.
`RESUME_MESSAGE` configurable (default `continue`).

### Limit-type handling
- Session (5h) limit: `status_detail` carries reset time ("resets 7:50pm
  UTC") → set `next_attempt_at` to just after.
- Org monthly limit: no reset time → capped backoff 5/15/30 min, retry until
  it clears.
- One resume per resume-tick, oldest first.

### State — `logs/paused-sessions.json`, keyed by `cse_` id
`{cse_id, env_kind, title, status_detail, first_seen, attempts,
last_attempt_at, next_attempt_at, resumed_at}`. Drop when the API no longer
reports it failed. GC after 7d.

### Loop / safety
- detect 60s, resume 300s, 1s tick (unchanged scaffolding: lock, SIGTERM,
  log).
- `DRY_RUN` env (default ON initially): detect + log "would resume", no POST.
- Guardrails: never resume `running`/`requires_action`; max-attempts cap;
  dedupe; skip self.

## Open questions
- True "retry" vs injected `continue` (the app's "Try again" may re-run the
  failed turn without adding a user message) — short spike to capture the
  app's event before locking `continue` in.
- Confirm `anthropic_cloud` sessions resume identically to `bridge` (POC
  proved `bridge`).
- Token refresh if all `claude` servers are down (v1 just waits/logs).
