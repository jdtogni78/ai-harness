---
name: take-over
description: >-
  Adopt a stale or disconnected session's work in a brand-new session.
  Spawns a fresh `oneoff-` worker anchored at the old session's cwd,
  delivers a reconstructed brief as its first turn, then acks the old
  session so it leaves the interrupted list. Distinct from resume-work,
  which continues work IN the current session — take-over always spawns
  FRESH and acks the old one. Use when the user says "take over session
  <UUID>", "take over stale session", "dispatch takeover for <UUID>",
  "hand off this stale session to a new worker", or "adopt the
  interrupted session in a fresh chat".
---

# take-over — adopt a stale session in a fresh worker

`resume-work` continues a stale session's work **here**, in the current
session. `take-over` does the opposite: it spawns a **new** `oneoff-` session
anchored at the old session's repo dir, delivers a reconstructed brief as its
first turn, and marks the old session as acked — all in one shot.

Use this when:
- A session died on API error / usage limit and you don't want to resume it
  yourself — you want a fresh worker to pick it up autonomously.
- A manager session is routing work to a worker and the worker went stale.
- You want the stale session out of the interrupted list immediately.

## Tool

`scripts/take_over.sh <UUID>` — implements the 4-step flow:

1. **Brief** — `resume_brief.sh <UUID>` extracts goal, last todo, files edited,
   last assistant message.
2. **Write prompt file** — brief + instructions → temp `.md` file.
3. **Spawn** — `python3 -m remote_control new-session --dir <cwd>
   --prompt-file <brief>` dispatches a `oneoff-` worker. The CLI auto-wires
   the caller's `cse_*` as `reply-to` so the worker reports back.
4. **Ack** — `resume_brief.sh ack <UUID>` marks the old session handled.

## Workflow

1. **Get the UUID.** Run `resume_brief.sh list` (or use the [[resume-work]]
   skill) to find the stale session's UUID.
2. **Verify the cwd still exists** (or that its parent does). If the worktree
   was already GC'd and the parent dir is wrong, pass `--dir` explicitly to
   `python3 -m remote_control new-session` instead of using the script.
3. **Run the script:**
   ```bash
   ~/.claude/skills/take-over/scripts/take_over.sh <UUID>
   ```
   On success it prints the new session's `cse_*` id and the ack confirmation.
4. **Track the worker.** The new worker's `cse_*` is your handle for follow-up
   messages via [[send-to-session]].

## When NOT to use

- **You want to continue the work yourself** → use [[resume-work]] instead.
- **The stale session is on another host** → send the UUID to the peer
  dispatcher via [[send-to-session]] and ask it to run take-over there.
- **The cwd is gone and the branch is merged** → just `resume_brief.sh ack`
  the UUID directly; there is nothing to hand off.

## Notes

- The script passes `--prompt-file` to `new-session`, which implies `--wait`
  and auto-detects `reply-to` from the calling session's JWT. The worker will
  report back to your session when it finishes.
- If `new-session` times out waiting for registration (default 30 s), the ack
  step is still run — check the worker's log manually and re-submit if needed.
- Acking is idempotent; running the script twice on the same UUID is safe
  (the second ack is a no-op).
