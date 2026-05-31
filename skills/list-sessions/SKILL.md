---
name: list-sessions
description: >-
  List the active (non-archived) Claude Code sessions, annotated with the repo
  each belongs to and the host/sandbox it runs on (this Mac, another machine, or
  an Anthropic cloud sandbox). Read-only inventory of what's live right now. Use
  when the user asks "list active sessions", "which sessions are running", "what's
  live right now", "which repos/hosts have sessions", "show non-archived
  sessions", or wants a cross-repo / cross-machine view of running chats. The
  resume-work skill calls this first to show what's already live before resuming.
---

# List active sessions (repo + host/sandbox)

The code-sessions API (`GET /v1/code/sessions`) returns archived and live
sessions mixed together. This surfaces just the **active (non-archived)** ones in
a scannable table so you can see what's running, for which repo, and on which
host/sandbox. **Read-only** — it never writes to a session (that's
[[rename-sessions]]).

## Tool

`python3 -m remote_control sessions` (from the repo root):

- `sessions` / `sessions list` (default) — active sessions, newest activity
  first, each with title, repo, host/sandbox, worker + connection state, and last
  event time, plus by-repo / by-host tallies.
- `--all` — include archived sessions too.
- `--repo <name>` — only sessions for this repo basename (case-insensitive).
- `--json` — machine-readable JSON array (one object per row, fields
  `id title repo env_kind location worker_status connection_status
  last_event_at`). Use this for programmatic flows (e.g. a manager scraping
  the cse_id of a specific worker) instead of regexing the text table.
- `--ids-only` — just the `cse_` ids, one per line (e.g. for piping into
  `fork-all --ids`).
- `--stale [--older-than DUR] [--disconnected]` — narrow to idle sessions
  whose `last_event_at` is older than the threshold (default 1h); add
  `--disconnected` to also require `connection_status=disconnected`.
- `--location this-host|other-host|cloud` — narrow by where the session runs.
- `--dev <DIR>` — dev root for bridge-worktree repo lookup (default `~/dev`).

Auth reuses the usage-limit monitor's keychain OAuth token; no extra setup.

### How repo + host are derived

**Repo** (first hit wins, shared with [[rename-sessions]]):
1. `config.sources[].url` → git repo basename (cloud / CLI-launched sessions).
2. a local bridge worktree `~/dev/<repo>/.claude/worktrees/bridge-<id>` → `<repo>`
   (bridge / app-launched sessions have an empty `config.sources`).

A session with neither is shown as `<unknown>`.

**Host / sandbox** (`environment_kind` + the local worktree index):
- `cloud` — `environment_kind == anthropic_cloud`: an Anthropic cloud sandbox,
  not tied to any local machine.
- `this host (<name>)` — a bridge session whose `cse_` id matches a local bridge
  worktree, so it runs on **this** Mac.
- `other host` — a bridge session with no local worktree: it runs on another
  machine. The API doesn't say which, so we can only report "not here". Run the
  same command on that machine to see it as `this host`.

## Workflow

1. Run `sessions` and show the table + tallies.
2. To narrow, add `--repo <name>`; to include archived, add `--all`.
3. For deeper triage of *interrupted* sessions (API failures, dirty worktrees,
   rescue) use [[session-triage]]; to *act on* one, use [[resume-work]].

## Notes

- `other host` is a limitation of the API, not a bug — bridge sessions carry no
  machine identity, so locality is inferred from whether their worktree exists
  here. The user runs agents across more than one Mac, so expect `other host`
  rows for sessions owned by the other machine.
- This is the read-only inventory counterpart to [[rename-sessions]]; both share
  the repo-derivation + worktree-index helpers in `remote_control/session_list.py`
  and `remote_control/session_titles.py`.
