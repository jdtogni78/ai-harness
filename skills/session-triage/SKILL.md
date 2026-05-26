---
name: session-triage
description: >-
  Triage Claude Code sessions and git worktrees for a repo: find sessions that
  hit API failures (ECONNRESET / synthetic "API Error") or ended mid-task,
  list dirty/unmerged worktrees, and non-destructively rescue uncommitted work.
  Use when the user asks "find sessions that ran here", "which sessions had API
  failures", "what worktrees are unfinished", "recover work from a crashed
  session", or wants resume/handoff prompts for interrupted sessions.
---

# Session & worktree triage

Claude Code logs every session for a repo as JSONL under
`~/.claude/projects/<slugified-repo-path>/` (path `/` → `-`). Worktree
sub-dirs are usually empty; the real log keys each entry by its `.cwd`.

API failures appear as assistant messages with `.message.model=="<synthetic>"`
whose text matches `API Error` (commonly `ECONNRESET`). A session that was
genuinely interrupted has such an error as its *last* timestamped entry.

## Tool

`scripts/session_scan.sh` (the canonical copy lives here; `~/.claude/session_scan.sh`
and `~/.codex/session_scan.sh` are back-compat symlinks to it):

- `session_scan.sh sessions [REPO]` — per-session table: end time, API-error
  count, whether it ended on an error (`ERR`), cwd, first prompt.
- `session_scan.sh worktrees [REPO]` — each worktree's branch, dirty file
  count, raw commits `ahead` of `main`, plus a content-diff verdict vs
  `main`'s merge-base (`git diff main...$br`): `MERGED` when the branch adds
  nothing to main (even if `ahead>0` — those are merge commits / already
  cherry-picked), or `UNMERGED` with a `diff=<N>f <I>+ <D>-` shortstat
  (files / insertions / deletions) when it carries real unmerged content.
  Trust the `MERGED`/`UNMERGED` verdict over `ahead`; `ahead` alone
  misclassifies squash-merged or merge-commit branches.
- `session_scan.sh rescue [REPO] [OUTDIR]` — for every dirty worktree, write
  `<name>.patch` (`git diff HEAD`), `<name>.status`, and copy untracked files.
  Non-destructive: never runs `git stash` or mutates the worktrees.

## Workflow

1. Run `sessions` and `worktrees` to map the situation.
2. Flag sessions with `LAST?=ERR` as interrupted; `MERGED` + non-dirty
   worktrees are disposable regardless of `ahead`, dirty or `UNMERGED`
   ones need rescue first.
3. Run `rescue` before suggesting any `git worktree remove`.
4. For each interrupted session, hand the user a resume prompt: point a fresh
   session at the transcript path and ask it to summarize what was done, then
   finish the task. Pull the first and last user message with jq to make the
   prompt specific.

`jq` is required. Prefer the script over ad-hoc greps so output stays stable.
