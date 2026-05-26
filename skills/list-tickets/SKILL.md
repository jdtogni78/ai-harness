---
name: list-tickets
description: >-
  List the tickets available to work on across all your GitHub Project boards,
  labeled with which project/board and repo each belongs to. Read-only — it
  surfaces work, it does not claim it. Use when the user asks "list possible
  tickets", "what tickets are available", "what's free to work on", "show open
  tickets", "what's on the boards", "which board/project does this ticket
  belong to", or "who's working what right now" (the In-Progress view).
---

# List tickets across the boards

A handful of GitHub Project boards (all under the same owner in a typical
setup) hold the work, and a single ticket lives on exactly one board. This
skill is the **read-only browse** step — it shows what's available and which
board/repo each ticket belongs to. To actually take one, hand off to
[[start-work-skill]] (claim) — this skill never mutates the board.

Your local mapping (which board number covers which repo) lives in
`~/dev/GITHUB_PROJECTS.md` ([[gh-projects-tracking]]) — the same file
[[start-work-skill]], [[resume-work-skill]] and [[close-work-skill]] all read.
A ticket on a board that isn't yours simply won't appear in `todo`.

## Tool

`../start-work/scripts/agent_claims.sh` — the shared board helper (same script
`start-work` / `resume-work` / `close-work` use; resolves `gh`/`jq` even
off-PATH). Two read-only views:

- **`agent_claims.sh todo`** — every **available** (Status = *Todo*) ticket
  across **all** boards, grouped by board and labeled with its repo. The
  "what's free to pick up" view. Each row is the issue # + repo (or `(draft)`
  for draft notes that have no issue number) + title.
  - `--project N` — narrow to one board (numbers per `~/dev/GITHUB_PROJECTS.md`).
  - `--all-projects` — explicit "sweep every board" (the default for `todo`).
- **`agent_claims.sh list [--all-projects]`** — In-Progress tickets and their
  **claim liveness** (`ALIVE` / `STALE` / `DEAD` / `OFFHOST`). The "who's
  working what, and are they still alive" view. Defaults to a single board;
  add `--all-projects` to sweep all configured boards.

The board(s) swept by `--all-projects` come from `AGENT_CLAIM_PROJECTS`
(a space-separated list of board numbers — set it to match your setup).

## Workflow

1. **Available work, all boards** (the usual ask):
   ```bash
   ~/.claude/skills/start-work/scripts/agent_claims.sh todo
   ```
   Or one board: append `--project <N>` (board numbers per
   `~/dev/GITHUB_PROJECTS.md`).

   Repos that carry a **domain/meta label** taxonomy on their issues
   (`security` · `ci` · `infra` · `acl` · `ui` · `ops` · `tooling` · `epic` ·
   `blocked`) are also filterable by label — see the Labels section in
   [[start-work-skill]]:
   ```bash
   gh issue list --repo <owner>/<repo> --label <label> --state open
   gh label list --repo <owner>/<repo>   # the full set
   ```
2. **Who's working what right now** (In-Progress + liveness):
   ```bash
   ~/.claude/skills/start-work/scripts/agent_claims.sh list --all-projects
   ```
3. **Want to take one?** Don't claim it here — switch to [[start-work-skill]]:
   `check <#>` to confirm it isn't held by a live agent, then `claim <#>
   --project <N>` (which mutates the board, so confirm with the user first).

## Rules

- **Read-only.** This skill lists; it must not comment on, assign, or move any
  ticket. Claiming is `start-work`'s job (and needs user confirmation).
- **Draft-note tickets** (boards where items have no underlying issue, e.g. a
  local-only repo) can't be claimed/liveness-tracked via the script; surface
  them but say so.
- Don't auto-pick a ticket for the user — present the list and let them choose
  ([[feedback_parallel_sessions]]: redirect to an existing session before
  starting fresh work).
