# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Hosts

Agents run across two machines, each with a short nickname: **note** (the
MacBook) and **mini** (the Mac Mini). Only **note** (the MacBook) has access to
production — **mini** (the Mac Mini) does not. Run any production task (deploys,
prod secret management, the SOPS/age prod runbook) from **note**.

## Project tracking

Work for this repo is tracked on the **Remote Control** GitHub Project board
(user-level project **#2**).

- Board: <https://github.com/users/youruser/projects/2>
- Add an issue to the board:
  ```bash
  gh issue create --repo youruser/ai-harness --title "..." --body "..."
  gh project item-add 2 --owner youruser --url <ISSUE_URL>
  ```
- Quick note without an issue: `gh project item-create 2 --owner youruser --title "..."`
- Open the board: `gh project view 2 --owner youruser --web`

Full setup notes: `~/dev/GITHUB_PROJECTS.md`. Managing boards via CLI needs the
`project` token scope (`gh auth refresh -s project --hostname github.com`).

## Cross-repo engineering decisions

Decisions that span more than one repo (the worktree workflow, multi-agent
ticket coordination, the env/test pools, secrets posture, etc.) live in
[DECISIONS.md](DECISIONS.md) (`GD-NNNN`). Read it before re-litigating a
cross-cutting convention; add a `GD-NNNN` entry when you make a new one.
Repo-specific decisions stay in that repo's own `docs/ENGINEERING_DECISIONS.md`.

### Multi-agent ticket coordination

Several agents (parallel Claude/Codex sessions, often across note + mini)
share this one board, so they coordinate via a **claim** convention to avoid
double-working a ticket and to recover one whose agent disconnected:

```
start-work (claim)  →  work  →  resume-work (if interrupted)  →  close-work (deliver / hand off)
```

A claim = **Status → In Progress** + **assignee `youruser`** + a structured
**claim comment** identifying the agent by **worktree + host**
(`youruser+<worktree>@<host>`). An agent is considered **disconnected** when its
session transcript has been idle ≥ **1h** (`AGENT_CLAIM_TTL_SECS`) or ended on
an API error / usage limit — then its `STALE`/`DEAD` claim is reclaimable.

Use the `start-work`, `resume-work`, and `close-work` skills; they share
`skills/start-work/scripts/agent_claims.sh`
(`whoami | check <#> | list | claim <#> | release <#>`). Details:
`skills/README.md` → *Multi-agent ticket coordination*.
