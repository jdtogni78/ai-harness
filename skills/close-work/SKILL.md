---
name: close-work
description: >-
  Wrap up a Claude Code thread cleanly: review every change made on this
  branch vs `main`, verify the right tests were actually run, surface
  remaining TODOs/half-done scope, sync the work's GitHub Project ticket, and
  then either deliver (commit → merge to `main` → push → release pool lease →
  remove worktree, or for a supervisor-managed bridge worktree switch it back
  to its bridge branch → release the agent claim → close the tracking ticket,
  per the no-PR workflow) or produce a handoff brief if the thread isn't ready
  to close.
  Use when the user says "close this thread", "close work", "wrap up", "we're
  done here", "finish and merge", "can we close this?", "ship it and clean
  up", or asks for a final review before ending the session.
---

# Close work — wrap up a Claude Code thread

This skill is the back half of the lifecycle ([[start-work-skill]] claims a
ticket → work → [[resume-work-skill]] if interrupted → `close-work` delivers).
It is the inverse of `resume-work`: it takes a session that thinks
it's done and answers two questions — **is it actually done?** and **if so,
deliver and tear down cleanly**. It assumes this repo's no-PR workflow (see
[[worktree-merge-no-pr-mode]]): delivery is commit → merge `--no-ff` into
`main` → push → release the preview lease → remove the worktree (or, for a
supervisor-managed `bridge-cse_*` worktree, switch it back to its bridge branch
instead of removing it — see Phase 5 step 7) → release the
agent claim and close the work's tracking ticket on its GitHub Project board
(see [[gh-projects-tracking]]; mapping + commands in `~/dev/GITHUB_PROJECTS.md`).

**Never auto-merge or auto-push.** Every state-changing step (commit, merge,
push, worktree removal, lease release, releasing/closing/moving a board ticket)
requires explicit user confirmation in this turn — a "close the thread"
request is not blanket approval for those actions.

## Phase 1 — Review the work

Goal: tell the user *exactly* what this thread changed, in terms they can
audit, before talking about closing anything.

1. **Where are we?** Capture `pwd`, current branch, and the worktree it
   belongs to (if any). If `pwd` is the main repo (not a worktree), say so —
   the merge/teardown steps below don't apply the same way.
2. **What changed vs `main`?** Run in parallel:
   - `git fetch origin main` (so the comparison is fresh — see
     [[concurrent-main-moves]])
   - `git log --oneline origin/main..HEAD`
   - `git diff --stat origin/main...HEAD`
   - `git status` (any uncommitted work?)
3. **Summarize to the user**: original goal of the thread (look at the first
   user prompt of this session if you can, else ask), commits made, files
   touched, any uncommitted changes. Flag anything surprising — generated
   files swept in, large diffs in unrelated areas, `.env.*` / preview-stack
   artifacts (see [[worktree-merge-no-pr-mode]] for the usual offenders).
4. **Find the tracking ticket.** This thread's work is usually tracked as an
   issue on a GitHub Project board (see [[gh-projects-tracking]]; mapping +
   commands in `~/dev/GITHUB_PROJECTS.md` and this repo's CLAUDE.md for which
   board covers which repo in your setup). Get its number from the branch
   name, first prompt, or commit messages; else search
   `gh issue list --repo <owner>/<repo> --search "<keywords>" --state open`.
   Note it — you'll close it in Phase 5. If this was real tracked work but no
   issue exists, flag that the board is out of sync and offer to file one now
   so delivery can close it. Board edits need the `project` token scope
   (`gh auth refresh -s project --hostname github.com`).

## Phase 2 — Test gate

Goal: don't let "tests passed earlier in the thread" be a stand-in for "the
right tests were run against the final state."

1. **Scan the transcript / recent tool output** for `php artisan test` or
   `bin/test.sh` invocations. Identify:
   - Were they run *after* the last code edit? (Edits after the last test
     run = the gate is stale.)
   - Did they cover the changed files (filter / suite scope)?
   - Did they pass?
2. **Tests must run in Docker** for this repo — `docker exec <app-container>
   php artisan test` or `bin/test.sh` (see CLAUDE.md). Running on host doesn't
   count.
3. If the gate is stale, missing, or the changes warrant broader coverage,
   **offer to run the relevant tests now** before talking about merge. Don't
   silently skip — call it out: "tests last ran at commit X, you've edited
   Y/Z since; run the suite now?"
4. For UI/Blade/Livewire changes, note that automated tests don't verify
   feature correctness — surface this and ask whether the user has clicked
   through the affected pages on the preview stack (see [[env-pool]] /
   [[worktree-preview-stack]]).

## Phase 3 — TODO sweep

Goal: nothing falls on the floor when the thread closes.

1. **Re-read this thread's chat history** end-to-end before the other
   sweeps below. You're looking for things that won't show up in git or
   TodoWrite: explicit asks the user made that you acknowledged but didn't
   finish ("also fix X" / "and add Y"), things *you* said you'd do ("I'll
   come back to that", "let me follow up on…"), pivots where scope shifted
   and the original ask got dropped, and quiet "let's defer that" moments.
   This step catches the silent backlog the other sweeps miss. List every
   such item with the user prompt or assistant message that originated it,
   so the user can verify your read.
2. **Last TodoWrite list** for this session — anything still `in_progress`
   or `pending`? Show it.
3. **In-code TODOs added this thread**: `git diff origin/main...HEAD` and
   look for `TODO`, `FIXME`, `XXX`, `@todo` introduced in this branch's
   diff. Don't flag pre-existing ones.
4. **Half-done scope vs the original goal**: re-read the first prompt. Did
   the thread deliver the whole ask, or a subset? If a subset, name what
   was deferred.
5. **Surface, don't silently file.** Show the user the consolidated list
   and ask how each should be handled:
   - *do it now* (loops back into normal work — the thread isn't actually
     closing yet),
   - *defer with a written follow-up* — the canonical home for a
     cross-session follow-up is a **GitHub issue on the right board** (Remote
     Control #2 / Trading & Fund #1):
     `gh issue create --repo <owner>/<repo> --title "…" --body "…"` then
     `gh project item-add <N> --owner youruser --url <issue-url>` (see
     [[gh-projects-tracking]] / [[project-followups-as-gh-issues]]). Reserve
     project memory or a `// TODO(handoff):` comment for context that doesn't
     warrant its own ticket, or
   - *drop it* (out of scope / no longer relevant).
   Only after the user decides should you write any follow-up artifacts.

## Phase 4 — Closeable verdict

Based on phases 1–3, give the user one of three verdicts, with reasons:

- **READY TO CLOSE** — clean tree, tests fresh and green, no unresolved
  TODOs/scope gaps, work either merged already or ready to merge. Proceed
  to Phase 5.
- **NOT READY** — name the blocker(s): dirty tree, stale/failing tests,
  unresolved TODOs the user wanted finished, or uncommitted scope. The
  thread should *not* close yet; recommend the next step.
- **READY TO HAND OFF** — work isn't finishing in this thread but should
  continue elsewhere. Write a handoff brief (see "Handoff brief" below) and
  leave the worktree intact so `resume-work` can pick it up.

State the verdict explicitly. Don't bury it.

## Phase 5 — Deliver (only if READY TO CLOSE, and user confirms)

This is the no-PR delivery path from [[worktree-merge-no-pr-mode]] with the
concurrency precautions from [[concurrent-main-moves]]. Confirm with the
user before each state-changing command — *especially* the push and the
worktree removal.

1. **Commit anything outstanding** (if dirty, after confirming what's being
   staged — never blanket `git add -A` without checking for pool/env
   artifacts).
2. **Re-fetch and check divergence immediately before merging**: `git fetch
   origin main` again — main may have moved while phases 1–4 ran. Expect a
   `--no-ff` merge, not a fast-forward.
3. **Merge into main**: switch to a local `main` checkout (worktrees often
   can't check out `main` directly — it's typically held by the main repo
   dir; do the merge there, or use `git push origin HEAD:main` only if
   it's truly fast-forward, which it usually isn't). Resolve conflicts
   carefully — a clean textual merge can still be a semantic conflict (see
   [[concurrent-main-moves]]).
4. **Re-smoke after merge**: if the change is user-facing, hit the affected
   page on the preview stack against the merged code before pushing. The
   preview bind-mounts the worktree, so the worktree branch must be
   fast-forwarded to the merge commit first.
5. **Push**: `git push origin main`. "Everything up-to-date" is not a
   failure — verify with `git branch -r --contains <sha>`.
6. **Release the env leases** (if any) — check **both** pools: the dev/preview
   pool (`$STATE_DIR/pool.sh list`) **and** the test pool
   (`$STATE_DIR/testpool.sh list`). (`$STATE_DIR` is the `state_dir` from your
   pool config — `scripts/pool/config.example.yaml`.) For each, find this
   worktree and `release <slot>` on the matching script (`pool.sh release
   <slot>` / `testpool.sh release <slot>`). A worktree that ran the suite via
   [[test-env]] holds a test slot that **leaks** if only the preview pool is
   released. Do this *before* removing the worktree dir, so the stacks tear
   down cleanly. See [[env-pool]] / [[pool-zombie-lease-gap]].
7. **Remove the worktree + branch**: only after the merge commit is on
   `origin/main`. **First check: is this a `bridge-cse_*` worktree?** (path
   under `.claude/worktrees/bridge-cse_*`, branch `worktree-bridge-cse_<id>`).
   Those are spawned and `locked` by the `claude remote-control` supervisor,
   and you may be *executing inside one* — **never `git worktree remove` it**;
   that deletes the live session's filesystem and breaks the session (see
   [[project-bridge-worktree-close]]). Instead "release" it by switching it
   back to its own bridge branch: `git checkout worktree-bridge-cse_<id>` (frees
   any feature branch you checked out into it), then `git branch -D
   <feature-branch>`. Leave the worktree dir in place — the supervisor manages
   it, not you.
   Otherwise (a normal feature worktree): `git worktree remove <path>` then
   `git branch -D <branch>` (safe because the merge proved zero unique content
   remains), and `git worktree prune` at the end.
8. **Release the agent claim, then close the tracking ticket** found in
   Phase 1, once the merge commit is on `origin/main`. The ticket was claimed
   for this agent by [[start-work-skill]] (a structured claim comment); on
   delivery, release it so the board doesn't show a dangling claim, then close:
   ```bash
   ~/.claude/skills/start-work/scripts/agent_claims.sh release <N> -- delivered in <merge-sha>
   gh issue close <N> --repo <owner>/<repo> --comment "Delivered in <merge-sha> (merged to main)"
   ```
   If the board doesn't auto-move closed items to **Done**, set the status
   explicitly (`gh project item-edit …` or the board UI). Also add to the board
   any follow-up issues you filed in Phase 3. Confirm before releasing/closing —
   see the no-auto rule above.
9. **Never delete the session JSONL** — it's durable history (see the
   "Leave transcripts alone" rule in [[resume-work-skill]]).

## Handoff brief (for READY TO HAND OFF)

Write a short note the user can paste into a fresh session, or save as a
`HANDOFF.md` in the worktree if the user prefers. It should contain:

- Original goal (verbatim from the first prompt is best).
- What was completed and verified, with commit SHAs.
- What remains, in concrete terms (file paths, function names, the specific
  failing test, the user-facing behavior still broken).
- Any non-obvious context: decisions made and why, dead ends ruled out,
  external state (pool lease still held, migration not yet run, etc.).
- Next concrete step a fresh agent should take.

This is the input `resume-work` will consume — write it for that audience.

**Release the claim on handoff.** This agent is going away, so its claim would
go stale anyway after the 1h TTL — but release it explicitly so the board shows
the ticket is up for grabs *now*, not in an hour, and `resume-work` (or another
agent's `start-work`) sees `RELEASED` instead of a soon-to-be-`STALE` claim:
`~/.claude/skills/start-work/scripts/agent_claims.sh release <N> -- handoff: <one-line reason>`.
Leave the ticket **In Progress** (the work continues, just elsewhere); the
release comment, not the status, is what signals it's available.

## Edge cases

- **Already merged**: if `git log origin/main..HEAD` is empty and the
  worktree branch is fully reflected on `origin/main`, the delivery is
  already done; skip to Phase 5 step 6 (release lease, remove worktree, then
  close the tracking ticket if it's still open).
- **No worktree (working in main repo)**: Phase 5 still applies for the
  commit/merge/push parts, but there's no worktree to remove and no lease
  to release. Just push and stop.
- **Concurrent worktree GC**: another agent may remove this worktree mid-
  task (see [[concurrent-worktree-gc]]) — commit dirty work to the branch
  *before* any slow review step in Phase 1, not after.
- **Tests can't run** (Docker down, container missing): say so explicitly
  in Phase 2 — don't claim a green gate. Offer to start Docker (see
  [[start-docker]]) or fall back to NOT READY.
