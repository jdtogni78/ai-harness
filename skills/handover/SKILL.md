---
name: handover
description: >-
  Abandon-cleanly path for an interrupted thread: commit whatever is committable
  on the current branch, file a tracking ticket on the repo's GitHub Project
  board with what's left + WIP location + exact resume instructions, then print
  a ≤3-line resume prompt (ticket # + worktree path). Use when the user says
  "/handover", "hand this off", "abandon this thread cleanly", "park this for
  later", "I have to stop — file the rest", or "drop a resume ticket and bail".
  Distinct from [[close-work]] (delivers/merges) and [[resume-work]] (picks the
  work back up).
---

# Handover — shortest-possible thread handover

The point of this skill is **brevity**. No reviews, no test gates, no scope
sweeps — that's [[close-work]]. Three steps, then exit.

## 1. Commit what's committable

- Capture `pwd`, current branch, `git status`.
- If the tree is dirty and stageable: `git add -A` (after a one-line check for
  obvious junk — `.env*`, pool artifacts), then
  `git commit -m "wip: handover — see #<ticket>"` once the ticket exists in
  step 2 (or amend the commit message after). Do **not** force-create a
  branch — commit on whatever branch is checked out, even `main`.
- If state is uncommittable (merge in progress, submodule conflict, etc.),
  **don't block**: note the exact state in the ticket body in step 2 and
  move on. The user will sort it on resume.
- Push the branch (`git push -u origin HEAD`) so the WIP is recoverable from
  any host. If push fails, note the local-only worktree path in the ticket.

## 2. File the tracking ticket

On the repo's GitHub Project board (see [[gh-projects-tracking]]; mapping +
commands in `~/dev/GITHUB_PROJECTS.md` — `gh` needs the `project` scope:
`gh auth refresh -s project --hostname github.com`).

Body template — keep it tight, no narrative:

```
**Was doing:** <one line — original goal>
**Left to do:** <bullets, concrete: file paths, failing test, next step>
**WIP:** branch `<branch>` @ <sha> on host `<host>`, worktree `<path>`
        (uncommitted: <none | describe>)
**Resume:** `cd <worktree> && git checkout <branch>` then `/resume-work`
```

Then:
```bash
gh issue create --repo <owner>/<repo> --title "handover: <short goal>" --body "<above>"
gh project item-add <N> --owner <owner> --url <issue-url>
```

If a tracking ticket for this work already exists, **comment on it** instead
of filing a new one — same body, prefixed `handover @ <sha>`.

## 3. Print the resume prompt

Three lines max — this is what the user pastes into a fresh session:

```
Resume #<N> (<repo>): worktree <path>, branch <branch>.
Read the ticket, then /resume-work.
```

Nothing else. No summary, no farewell. Exit.

## Rules

- **Never merge, never delete worktrees, never release leases.** That's
  [[close-work]]. Handover assumes the work is *unfinished*; the worktree and
  any pool leases stay put so [[resume-work]] can pick them up.
- **Release the agent claim if one exists** so the board doesn't show a
  dangling claim past the 1h TTL:
  `~/.claude/skills/start-work/scripts/agent_claims.sh release <N> -- handover: <one-line reason>`
  (only if the work was previously claimed via [[start-work]]). Leave the
  ticket itself **In Progress** — the release comment, not the status, signals
  it's up for grabs.
- **One confirmation, not many.** Show the user the planned commit message,
  ticket title+body, and resume prompt in one block; on OK, run all three
  steps without re-prompting.
- **Don't delete the session JSONL.** Transcripts are durable history.
