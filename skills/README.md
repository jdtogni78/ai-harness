# skills/

Canonical, version-controlled home for the Claude Code / Codex skills used with
this repo's workflow. This directory is the **single source of truth**; the
per-tool global skill dirs are symlinks back here, so a skill is edited once and
picked up by both tools.

## Skills

| Skill                 | Purpose |
|-----------------------|---------|
| `remote-control-dirs` | Manage the allowlist of dev dirs the supervisor spawns servers for (`active-dirs.txt`). |
| `rename-sessions`     | Bulk-prefix Claude Code session titles with a per-repo nickname (`[MYAPP] ...`) so chats group by repo (`python3 -m remote_control titles`). |
| `list-sessions`       | Read-only inventory of active (non-archived) sessions, annotated with repo + host/sandbox (this Mac / other machine / cloud) (`python3 -m remote_control sessions`). |
| `session-triage`      | Find sessions that hit API failures / ended mid-task; list dirty worktrees; rescue work. |
| `list-tickets`        | Read-only browse of available (Todo) tickets across **all** boards, labeled by project + repo (plus the In-Progress "who's working what" view). |
| `start-work`          | Pick a Todo ticket, verify it isn't held by a live agent, then **claim** it (In Progress + claim comment). |
| `resume-work`         | Resume an interrupted session's work in a fresh chat; clean up landed worktrees + pool leases. |
| `close-work`          | Wrap up a thread: review vs `main`, sync the GH Project ticket, release the claim, deliver or hand off. |
| `lease-env`           | Lease a shared **preview** env from your app's pool (slot names per `scripts/pool/config.example.yaml`). |
| `test-env`            | Lease an isolated-DB **test** slot from your app's test pool (slot names per `scripts/pool/config.example.yaml`). |

`session-triage/scripts/session_scan.sh`, `resume-work/scripts/resume_brief.sh`,
and `start-work/scripts/agent_claims.sh` are the real scripts (no external
symlinks).

## Multi-agent ticket coordination

Several agents (different Claude/Codex sessions, often across note + mini)
share one GitHub Project board, so they need a way to not double-work the same
ticket — and to recover a ticket whose agent disconnected. The lifecycle:

```
start-work (claim)  →  work  →  resume-work (if interrupted)  →  close-work (deliver / hand off)
```

- **Claim model — hybrid, GitHub-anchored** (one shared source of truth, syncs
  across machines for free): **Status → In Progress** (canonical "being
  worked"), **assignee → `youruser`** (coarse "claimed" filter — one GitHub
  account, so it can't say *which* agent), and a **structured claim comment**
  carrying the agent identity. The identity anchors on **worktree path + host**
  (token `youruser+<worktree>@<host>`) — the reliable join key, since the readable
  `cse_` id lives only in the worktree path while the transcript filename is an
  unrelated UUID.
- **Disconnect detection — passive, 1h TTL:** an agent is *alive* if its session
  transcript (`~/.claude/projects/<slug>/`) was appended within
  `AGENT_CLAIM_TTL_SECS` (default 3600s) and didn't end on an API error / usage
  limit. Otherwise its claim is `STALE`/`DEAD` and another agent may reclaim it.
  Cross-host claims read `OFFHOST` (the transcript is local to the claimer).
- **The helper:** `start-work/scripts/agent_claims.sh`
  (`whoami | check <#> | todo | list | claim <#> | release <#> | set-status <#> "<S>"`)
  is shared by all the ticket skills. `whoami`/`check`/`todo`/`list` are
  read-only (`todo` = available Todo tickets across boards via [[list-tickets]];
  `list` = In-Progress + liveness); board status edits need the `project` token
  scope (`gh auth refresh -s project`).

## Linking globally

Run once per machine to (re)create the symlinks for both Claude and Codex:

```bash
skills/link.sh
```

It symlinks each skill into `~/.claude/skills/` and `~/.codex/skills/`, replacing
stale copies and leaving correct links untouched. It does not touch tool-specific
skills that don't live here (e.g. Codex's `playwright`). It also creates
back-compat symlinks for the bare `~/.claude/session_scan.sh` /
`~/.codex/session_scan.sh` paths.
