---
name: resume-work
description: >-
  Resume an interrupted Claude Code session's work in a fresh chat. Lists the
  repo's recent/interrupted sessions+worktrees so the user can pick one, then
  reconstructs the original goal, what was done, and what remains, checks the
  GitHub Project board for the work's tracking ticket (and moves it to In
  Progress), verifies/rescues its worktree, and continues the task. Also
  cleans up worktrees whose work actually landed (MERGED + clean) and reclaims
  the preview-pool leases those (and any zombie) slots hold, so they stop
  cluttering the list. Use
  when the user says "resume the work", "restore the session/worktree", "pick
  up where session X left off", "continue the interrupted task in a new chat",
  "clean up the finished worktrees/sessions", or "review/clean up the
  preview/pool leases".
---

# Resume interrupted work

`session-triage` *discovers and diagnoses* stranded sessions/worktrees. This
skill *acts on one*: it rebuilds enough context from a single session's
transcript + its worktree to safely continue the task here.

## Tool

`scripts/resume_brief.sh` — read-only; never mutates git. Modes:

- **`resume_brief.sh list [--all] [N]`** (also the no-arg default) — numbered
  table of the repo's recent sessions (incl. worktree project dirs): ended
  time, `ERR` if it died on an API error, worktree name, dirty count, original
  goal, and each session's UUID to pass back. `N` caps rows (default 15).

  By default, two classes of sessions are **auto-hidden** so the list shrinks
  to what actually needs attention:
  1. **Done**: cwd dir is gone AND the worktree's branch (`worktree-<basename>`)
     is either also gone or has zero unique content vs `main`. This is the
     same signal `session_scan.sh worktrees` uses to declare `MERGED`+clean.
  2. **Acked**: UUIDs the user explicitly dismissed via `ack` (below).

  The footer reports how many were hidden in each bucket. Pass `--all` to
  show everything (e.g. for archeology or when auto-detection seems off).
- **`resume_brief.sh ack UUID [UUID...]`** / **`unack UUID [UUID...]`** — mark
  sessions as handled (hidden from list) / undo. Use this for sessions whose
  worktree still exists but you've decided to abandon, or any session you
  want out of the way without auto-detection. Acked UUIDs are stored per-repo
  in `~/.claude/projects/<slug>/.resume-acked`.
- **`resume_brief.sh SELECTOR`** — full briefing for one session. SELECTOR =
  a `.jsonl` path, a session UUID, a worktree dir name/path, or any substring
  of the session's cwd or first prompt. Prints transcript path, cwd/worktree,
  branch + dirty count, git status, **original goal**, recent prompts, last
  assistant message, last TodoWrite list, and files Edited/Written.

## Workflow

1. **See what's live, then list and let the user pick.** First run
   `python3 -m remote_control sessions` (the [[list-sessions]] skill) for a
   cross-repo / cross-host view of the **active (non-archived)** sessions —
   what's already running, for which repo, on this Mac vs. another machine vs. a
   cloud sandbox. This is the at-a-glance "is someone already on this?" check
   that complements the per-ticket liveness gate in step 2: if the work you'd
   resume is already a live session here (or `other host`), prefer pointing the
   user there over duplicating it ([[feedback_parallel_sessions]]). If the
   session in question is `disconnected` rather than merely interrupted (a
   picker row with a warning triangle), run `python3 -m remote_control
   takeover` first — see [[takeover]] — to batch-classify and handle every
   disconnected/stale session at once (relaunching real work, archiving dead
   ends), then come back here with `resume-work` for the specific one you
   want to continue interactively. Then run
   `resume_brief.sh list` for the per-repo interrupted-session table and show it.
   **Always let the user choose** which session/worktree to resume — don't
   auto-pick. If the user already named a specific feature/complaint or UUID, you
   may skip straight to step 2 with that as the SELECTOR. For deeper triage
   (unmerged vs. merged worktrees, rescue) use the `session-triage` skill.
2. **Brief.** Run `resume_brief.sh <chosen UUID/selector>` and read it. Then
   check the GitHub Project board for this work's tracking ticket (see
   [[gh-projects-tracking]]; mapping + commands in `~/dev/GITHUB_PROJECTS.md`
   for which board covers which repo in your setup) —
   `gh issue list --repo <owner>/<repo> --search "<keywords>"`. Open
   follow-ups from earlier triage already live there as issues (see
   [[project-followups-as-gh-issues]]), so the matching ticket is often the
   crispest statement of what remains — read it alongside the transcript.

   **Liveness gate — don't stomp a live agent.** Once you've found the ticket #,
   run `~/.claude/skills/start-work/scripts/agent_claims.sh check <issue#>`
   before doing anything. The work you're about to resume may be claimed by
   another agent that's *still active* (a parallel session on this or another
   machine):
   - **ALIVE** → **stop.** Another agent is on it right now (active <1h). Point
     the user at that session/host rather than duplicating work — this is the
     [[feedback_parallel_sessions]] rule. Only take over if the user explicitly
     decides to.
   - **STALE / DEAD** → the claiming agent disconnected (≥1h idle, or its
     session ended on an API error / usage limit). Safe to take over — you'll
     re-claim it in step 6.
   - **OFFHOST** → claimed on another machine and not verifiable here; surface
     it and let the user decide.
   - **RELEASED / NOCLAIM** → free; proceed.
3. **Locate the worktree.** Use the briefing's `cwd`. If it still exists, the
   real work to continue is **there**, not in the current dir — operate
   against that path (or have the user open a session rooted there; flag this
   explicitly when cwd ≠ your cwd). If cwd is gone, the worktree was GC'd —
   the branch may still hold the commits; check `git branch -a`.
4. **Rescue before touching anything.** If dirty, run session-triage's
   `session_scan.sh rescue` first (non-destructive) so uncommitted work is
   captured even if something goes wrong.
5. **Reconcile with `main`.** Concurrent bridge agents may have already landed
   overlapping work (see [[concurrent-main-moves]]). Check `git log main`
   and diff before redoing anything — treat this as *verify-then-finish-the-
   gap*, not a blind redo.
6. **Confirm scope, then continue.** Summarize to the user: original goal →
   what was done → what's verified-on-main → the genuine remaining gap.
   Reconcile that gap with the tracking ticket's description (the ticket may
   list sub-items the transcript doesn't). Confirm the gap, then — with the
   user's OK — **re-claim the ticket for this agent**:
   `~/.claude/skills/start-work/scripts/agent_claims.sh claim <issue#>`. This
   posts a fresh claim comment (now pointing at *your* worktree/host) and moves
   the ticket to **In Progress**, so concurrent agents see it's being worked and
   the stale claim from the disconnected agent is superseded. Do the work in the
   correct worktree and verify (tests / dev stack / browser) before claiming
   done. When the work lands, `close-work` releases the claim and closes the
   ticket; if you finish and merge here, do that yourself.

## Cleanup (worktrees/sessions that actually ended their work)

Sessions that *finished* — their work merged to `main` — leave a clean,
MERGED worktree behind that clutters the list and hides the ones that still
need attention. Pruning these is now part of this skill (it used to be
deferred to `session-triage`). Do it whenever the user asks to "clean up"
finished work, or proactively offer it after step 1 if the list shows
disposable worktrees.

A worktree is **disposable** only when `session_scan.sh worktrees` reports
it as **`MERGED` with `dirty=0`** — the content-diff verdict (`git diff
main...$br` empty), *not* `ahead`, proves the branch adds nothing to `main`.
Anything `UNMERGED`, dirty, or ambiguous is *not* finished work — leave it
for resume/rescue, never clean it.

C1. **Rescue first, always.** Run `session_scan.sh rescue` across the repo
    before removing anything (non-destructive; a no-op for clean trees but
    the safety net costs nothing and guards against a misread verdict).
C2. **Exclude the untouchable.** Never remove your own current worktree, the
    main repo, or a worktree the user just chose to resume.
C3. **List exactly what will go, then confirm.** Show the user the precise
    `MERGED`+clean worktrees and their branches and get explicit approval —
    `git worktree remove` + branch deletion is hard to reverse. Concurrent
    GC/bridge agents may move things (see [[concurrent-worktree-gc]] /
    [[concurrent-main-moves]]); re-run the scan if there's any lag between
    confirm and removal.
C4. **Remove.** For each approved one: first release any env lease it holds,
    checking **both** pools — `$STATE_DIR/pool.sh list` (dev/preview)
    **and** `$STATE_DIR/testpool.sh list` (test). (`$STATE_DIR` is the
    `state_dir` from your pool config — `scripts/pool/config.example.yaml`.)
    If a row's `worktree` is under `<path>`, `release <slot>` on the matching
    script (`pool.sh` / `testpool.sh`; slot name works from anywhere) so the
    stack is torn down before the dir vanishes. Then `git worktree remove
    <path>` and `git branch -D <branch>`. `-D` is safe here *because* the
    MERGED diff verdict already proved zero unique content (`git branch -d`
    would wrongly refuse squash/merge-commit branches). Finish with `git
    worktree prune`.
C5. **Sweep orphaned + zombie leases (both pools).** Run `gc` on each —
    `$STATE_DIR/pool.sh gc` **and** `$STATE_DIR/testpool.sh gc`
    (docker must be on PATH — see [[pool-sh-docker-path-gc]]) to reclaim
    leases whose worktree is gone. `gc` does **not** catch a *zombie* lease:
    slot still `leased`, worktree dir present, but its per-slot app container
    (`<container_prefix>-<slot>` from the pool config) is `Exited` (still in
    `docker ps -a`, so gc skips it) — the stack is dead but the slot looks
    taken. Detect these by diffing each pool's `list` (`leased` rows) against
    `docker ps` (only *running* containers, filtered by the pool's
    `container_prefix` — e.g. `<prefix>-pool*` for the dev pool, `<prefix>-test*`
    for the test pool); for each leased slot with no running container,
    confirm with the user, then `release <slot>` on the matching script.
    Never release the slot of the worktree being resumed, your own, or the
    main dir without asking — a live owner can re-up an Exited stack. See
    [[env-pool]].
C6. **Leave transcripts alone.** "Cleaning up a session" means retiring its
    orphaned worktree + branch — the session JSONL is durable history; never
    delete transcripts.

## Notes

- The briefing is a *starting point*. For deep history, read the transcript
  JSONL directly (it's the `transcript` path in the briefing).
- **Keep the board in sync.** Resuming work → move its ticket to *In
  Progress*; if cleanup (above) finds a `MERGED`+clean worktree whose ticket
  is still open, that work already landed — offer to close the ticket (→
  *Done*) so the board matches reality. Board mapping + commands:
  [[gh-projects-tracking]] / `~/dev/GITHUB_PROJECTS.md`; board edits need the
  `project` token scope (`gh auth refresh -s project`).
- Cleanup (the Cleanup section above) only ever touches `MERGED`+clean
  worktrees, and only after `rescue` + explicit user confirmation. While
  *resuming* a specific task, still never `git worktree remove` the worktree
  you're continuing — finish/verify it first.
- Lease cleanup (C4–C5) is independent of worktree removal: a user may ask
  only to "review/clean up the leases". In that case run just the `list` vs
  `docker ps` reconciliation + `gc` for **both** pools (`pool.sh` +
  `testpool.sh`), releasing orphaned/zombie slots with confirmation — no
  worktree/branch deletion.
- `jq` and `git` required.
