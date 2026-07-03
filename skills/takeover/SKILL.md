---
name: takeover
description: >-
  Batch-handle a picker full of disconnected/stale sessions in one shot via
  `python3 -m remote_control takeover`: for each candidate, read its last
  user turn and classify it as real work in progress (relaunch a fresh
  bridge + retitle + archive the original) or a dead end (archive only),
  then print a summary table. Distinct from `take-over` (hyphenated), which
  adopts ONE named session into a fresh worker via new-session; this skill
  is the batch triage pass across ALL stale/disconnected sessions at once.
  Use when the user says "clean up the disconnected sessions", "take over
  the stale sessions", "the picker has a bunch of warning triangles", "batch
  relaunch my dead sessions", or "run takeover".
---

# takeover — batch-handle stale/disconnected sessions

When the picker accumulates several warning-triangle (disconnected/stale)
sessions at once, going through [[resume-work]] or `take-over` one at a time
is slow. `python3 -m remote_control takeover` scripts the whole triage pass:
find every candidate, read what it was doing, decide relaunch-vs-archive, and
act — in one command.

## When to use

- The picker shows a handful of disconnected/warning-triangle sessions and
  you want them all handled, not just one.
- [[resume-work]]'s step 1 flags a session as `disconnected` (not merely
  interrupted) — run `takeover` first to clear the backlog, then come back
  to `resume-work` for the specific session you want to continue in this
  chat.

## Command

```bash
python3 -m remote_control takeover [--dry-run] [--older-than DUR] [--dev DIR]
```

- `--dry-run` — classify and print decisions only; no relaunch/archive/rename.
  **Always run this first** and review the table before the live run.
- `--older-than DUR` — staleness threshold for the time-based check (default
  `1h`), e.g. `30m`, `2h`, `1d`. A session that's merely `disconnected` (not
  yet stale-by-time) is *always* a candidate regardless of this threshold —
  connection state and staleness are checked independently, not combined,
  because a session can disconnect while still recently active (a gap the
  plain `sessions --stale --disconnected` filter misses).
- `--dev DIR` — dev root for bridge-worktree repo lookup (default `~/dev`).

## What it does, per candidate

1. **Find candidates**: active (non-archived) sessions that are idle AND
   (disconnected OR stale-by-time).
2. **Read the last user turn**: via the same source resolution `relaunch
   --show-brief` uses (local transcript scan, falling back to the events
   API for cloud/cross-host sessions) — there's no `messages` subcommand.
3. **Classify**: a last turn of ≤10 words (or none recoverable) is judged
   trivial → `archive-only`; anything longer → `relaunch`.
4. **Act**:
   - `relaunch`: spawns a fresh bridge (`relaunch --from CSE_ID`), then
     retitles the spawn to carry the *source's own* `[NICK...]` bracket
     verbatim (`titles set --id NEW_CSE --nick <source's nick>`) — the
     spawn is an other-host bridge from this dispatcher's point of view, so
     the titles watcher has no local worktree/transcript to derive a repo
     from and would otherwise leave the title bracket-less. Then archives
     the original now that its work has a live successor.
   - `archive-only`: archives the original directly.
5. Prints a `relaunched: N  archived: N  failed: N` summary.

## Manual fallback (edge cases the script doesn't cover)

If a candidate needs judgment the auto-classifier can't make (e.g. the last
turn is long but clearly just chatter, or the source transcript is
unreadable), fall back to the 7-step manual sequence:

1. `python3 -m remote_control sessions --json` — list candidates
   (`connection_status=disconnected` or stale `worker_status=idle`).
2. `python3 -m remote_control relaunch --from <cse_id> --show-brief --dry-run`
   — read the "Last N user turn(s)" section to judge real work vs. no-op.
3. Decide: real work → relaunch; only "hi"/replaced dispatcher/days-stale →
   archive-only.
4. `python3 -m remote_control relaunch --from <cse_id>` — relaunch.
5. `python3 -m remote_control sessions archive <cse_id> [<cse_id>...]` —
   archive the no-work sessions.
6. `python3 -m remote_control sessions archive <old_cse_id>` — archive the
   original(s) whose work was relaunched.
7. Fix the new session's title if the watcher couldn't derive `[NICK]` for it
   (always true for other-host spawns):
   ```bash
   python3 -m remote_control titles set --id <new_cse_id> \
     --nick "<NICK.host>" "<description>"
   ```
   `--nick` sets the bracket prefix verbatim, skipping repo derivation
   entirely — the fix for the exact gap this manual step used to route
   around via direct Python API calls to `session_titles.set_title`.

## Notes

- Known gap: no `messages` subcommand exists; `relaunch --show-brief
  --dry-run` (and this script's `last_user_turn`) are the only way to read a
  session's history short of opening the transcript JSONL directly.
- `relaunch` itself does not rename its spawn beyond inheriting the source's
  version-bumped body under whatever bracket the spawn already has (usually
  bracket-less for an other-host spawn) — `takeover` is what fixes the
  bracket, using `titles set --id ... --nick ...`.
- Safe to re-run: relaunching a source that already has a handoff record is
  a no-op (relaunch's own idempotency gate), and archiving an already-archived
  session just returns non-200 (counted as a failure, not retried).
