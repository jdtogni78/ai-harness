---
name: relaunch
description: >-
  Recover a stopped / crashed / unresumable Claude Code session by spawning a
  fresh bridge in its original cwd, seeded with a brief auto-derived from the
  prior session's transcript (cwd + branch + last N user turns) as the new
  session's first user turn. The new session inherits the source's title with
  a `vN` version bump (`Resume prep` → `Resume prep v2`). Wraps `python3 -m
  remote_control relaunch`, the stand-alone CLI form of the same handoff flow
  the supervisor runs on restart (`handoff.run_handoff_dispatch`). Use when
  the user says "relaunch session X", "this session is stuck / crashed,
  recover it", "spawn a fresh copy with context", "/relaunch cse_...", or
  asks for the manual equivalent of the supervisor's restart handoff. Do NOT
  use for a live but paused-on-usage-limit session (the usage-limit monitor
  auto-resumes those — see [[send-to-session]]'s "When NOT to use").
---

# relaunch (handoff a broken session into a fresh bridge)

A stopped or unresumable `cse_*` session cannot accept `/events` POSTs — the
events API errors, and the in-cloud agent isn't coming back. The supervisor's
restart path handles this with a **handoff**: spawn a brand-new bridge in the
prior session's cwd, derive a brief from its transcript, and submit the brief
as the first user turn so the new session can pick up where the old one
stopped. This skill exposes the same flow as a one-shot CLI for cases the
supervisor's auto-handoff doesn't cover (e.g. a session that died but the
supervisor isn't being restarted, or you explicitly want to fork an active
session into a "v2" copy without nuking the original).

The composition under the hood:

1. **Brief** — `handoff.derive_brief_from_transcript` reads the source's
   `<uuid>.jsonl` from `~/.claude/projects/<encoded-cwd>/` and produces a
   header (cwd + branch + transcript path) + the last N user turns. 8 KiB
   hard cap; the oldest turns are dropped first when over budget.
2. **Spawn** — `new_session.main` is invoked with `--prompt-file <brief>
   --no-reply-to --subname relaunch-<short-id>` against the source's cwd, so
   the new bridge registers in the same project + worktree the original ran in.
3. **Retitle** — after the new `cse_*` registers and the brief is delivered,
   the source's current title (e.g. `[JOB.mini] Resume preparation`) is
   fetched, its body is `bump_version`'d (`Resume preparation` → `Resume
   preparation v2`), and `set_title` PUT's the new title onto the new
   session, preserving the spawn's `[NICK.host][relaunch-XXX]` prefix.
4. **Ledger** — a per-cse handoff record lands under
   `~/.ai-harness/handoffs/<new-cse>.json` keyed on the *source* `cse_*`. A
   second `relaunch` against the same source is refused (idempotency gate;
   `--force` overrides).

## Tool

```
python3 -m remote_control relaunch
    (--from CSE_ID | --from-transcript PATH)
    [--cwd PATH] [--max-turns N] [--max-bytes N]
    [--wait-timeout SECS] [--state-dir PATH]
    [--show-brief] [--force] [--no-retitle] [--dry-run]
```

- `--from CSE_ID` — source session whose transcript seeds the brief. The
  transcript is resolved via a `~/.claude/projects/*/` scan
  (`session_fork.cse_id_from_project_dirname` is the matcher), so a
  user-facing `cse_*` is enough; no need to find the file by hand.
- `--from-transcript PATH` — explicit transcript path (the user already
  forked or relocated it). Pair with `--cwd PATH` if the transcript carries
  no `cwd` record; otherwise the embedded cwd is used.
- `--cwd PATH` — override the spawn cwd. Use sparingly; the transcript's
  first `cwd` is the authoritative answer.
- `--max-turns N` (default 5) — user turns to quote from the prior session.
  Matches `handoff.DEFAULT_MAX_USER_TURNS`.
- `--max-bytes N` (default 8192) — hard cap on the brief. Matches
  `handoff.DEFAULT_MAX_BRIEF_BYTES`. Going over silently drops oldest turns.
- `--wait-timeout SECS` (default **60**) — how long to poll for the new
  `cse_*` to register. The plain `new-session` CLI defaults to 30s, which
  misses slow registrations under load; relaunch's 60s default matches the
  supervisor's `handoff.DEFAULT_WAIT_TIMEOUT_SECS`.
- `--state-dir PATH` — where the handoff record is written. Default is
  `$REMOTE_CONTROL_STATE_DIR` or `~/.ai-harness`.
- `--show-brief` — print the derived brief to stdout (or after `--- brief ---`
  in dry-run output). Useful to sanity-check what the new session will see
  before firing.
- `--force` — bypass the idempotency gate. Use when you've cleaned up the
  prior failed handoff out of band and want a fresh attempt.
- `--no-retitle` — leave the spawn's default `[NICK.host][relaunch-XX]
  auto-spawned` title in place rather than inheriting + version-bumping the
  source's. Default is **on** — the user wanted relaunch to track its
  lineage in the picker.
- `--dry-run` — print the resolved source, cwd, brief metadata, and the
  spawn argv. No spawn, no record write, no network.

Auth reuses the usage-limit monitor's keychain OAuth token.

## When to use vs. neighboring skills

| Situation | Skill |
|---|---|
| Session is stopped/crashed/unresumable, want to pick up the work | **relaunch** (this skill) |
| Session is alive and idle, want to deliver a message into it | [[send-to-session]] |
| Session is paused on a usage limit | **Do nothing.** The usage-limit monitor auto-resumes it; manual nudging steps on its backoff. |
| Need to inspect a prior session before deciding what to do | [[resume-work]] (read-only triage), then come back here |
| Want a fresh worker for new work (no prior context to seed) | [[new-session]] |

The first row is this skill's whole reason to exist: a relaunch is **not**
"resume from the same transcript" (that path is fragile and the agent often
won't come back) — it's "spawn a fresh agent with enough breadcrumbs to
continue". You trade exact transcript continuity for reliability.

## Workflow

1. **Confirm the source needs relaunching.** Run
   ```
   python3 -m remote_control sessions list
   ```
   ([[list-sessions]]) and look at the target's `state` line. A
   `worker=running` or `worker=idle` session does NOT need relaunching;
   relaunch the ones that show as stuck/dead/disconnected, or one whose
   transcript ended on an API error (use [[session-triage]] to find these).
2. **Dry-run.**
   ```
   python3 -m remote_control relaunch --from cse_XXX --dry-run --show-brief
   ```
   Show the user the resolved cwd + brief content. If the cwd looks wrong
   or the brief is empty, ask before proceeding.
3. **Confirm and fire.** Drop `--dry-run`. The CLI prints:
   ```
   submitted cse_NEW (<N> chars)
     title  : '[NICK.host][relaunch-XX] Resume prep v2'
   relaunch: cse_OLD -> cse_NEW (record=~/.ai-harness/handoffs/cse_NEW.json)
   ```
4. **(Optional) Park or archive the source.** Relaunch doesn't touch the
   source session. If you want it out of the picker, archive it separately
   (`python3 -m remote_control sessions archive cse_OLD` if available, or
   use the desktop app). The retitle's `vN` bump is the visible signal that
   the original was superseded.

## Confirm before firing

Relaunch spawns a real picker-visible bridge server, submits a turn into it,
and overwrites its title. All three are side-effecting. Always:
- show the user the dry-run output first;
- only drop `--dry-run` after they confirm;
- never relaunch a session whose `worker=running` (the original is still
  doing the work; relaunch makes a duplicate that races).

## Examples

Standard recovery of a stopped session:

```
python3 -m remote_control relaunch --from cse_015ZNHAuJi5RoSP2a27ao49d
```

Inspect first, then commit:

```
python3 -m remote_control relaunch --from cse_015ZNHAu --dry-run --show-brief
python3 -m remote_control relaunch --from cse_015ZNHAu
```

A fresh attempt after a prior handoff failed and was cleaned up:

```
rm ~/.ai-harness/handoffs/<old-handoff>.json
python3 -m remote_control relaunch --from cse_015ZNHAu --force
```

Relaunch from an explicit transcript (e.g. one you `fork`-ed manually):

```
python3 -m remote_control relaunch \
    --from-transcript ~/.claude/projects/.../abc.jsonl \
    --cwd /Users/me/dev/foo
```

## When NOT to use this skill

- **A live session you can still talk to.** Submit a turn via
  [[send-to-session]] instead — relaunch will spawn a *duplicate*, and the
  user will end up with two bridges in the picker for the same work.
- **A session paused on a usage limit.** The usage-limit monitor's
  `attempt_resume` already nudges it back on the same `cse_*` once the
  limit clears; relaunching would fork the work onto a new id and confuse
  the recovery.
- **You don't actually want to continue the same work.** Use [[new-session]]
  — relaunch hard-codes the source's cwd + brief, which is wrong for
  unrelated work.
