# Cross-Repo Engineering Decisions

The canonical, living log of decisions that span **more than one repo** in this
workspace — the conventions and shared tooling that govern `app-two`,
`app-two-aws` (cloned as `~/dev/app-two-docker`), `app-two_python`,
`AppTwoAnalysis`, `AppOne`, `job-search`, and `ai-harness` itself.

It is the cross-cutting sibling of each repo's own `ENGINEERING_DECISIONS.md`
(ADR-lite, `ED-NNNN`). **Repo-specific decisions stay in that repo's ED log**;
only decisions that are true across repos — or about the shared harness/tooling —
belong here.

**How to use this file**
- Add a `GD-NNNN` (General Decision) section when a cross-repo convention is
  non-obvious, hard to reverse, or future-you (on any repo, on any machine)
  would otherwise re-litigate it.
- Never supersede in place — add a new entry and flip the old one's **Status** to
  `Superseded by GD-NNNN`.
- This file is an **index of decisions + pointers to where the real docs/code
  live**, not a place to duplicate those docs. Link out; don't copy.
- Keep secret VALUES out of this file (decisions only).

## Per-repo ED logs (decisions that are NOT cross-repo)

| Repo | Decision log |
|---|---|
| AppOne | `AppOne/docs/ENGINEERING_DECISIONS.md` |
| (others) | add a `docs/ENGINEERING_DECISIONS.md` as they accrue non-obvious decisions |

## Index

| ID | Date | Status | Decision |
|----|------|--------|----------|
| [GD-0001](#gd-0001--github-projects-not-jira-one-board-can-span-repos) | 2026-05 | Accepted | GitHub Projects (not Jira) for tracking; one board can span repos |
| [GD-0002](#gd-0002--worktree-per-task-never-switch-the-canonical-checkouts-branch) | 2026-05 | Accepted | Worktree-per-task; never switch the canonical checkout's branch in place |
| [GD-0003](#gd-0003--multi-agent-ticket-coordination-via-a-claim-convention) | 2026-05 | Accepted | Parallel agents coordinate via a claim convention (claim → work → resume → close) |
| [GD-0004](#gd-0004--remote-control-supervisor-driven-by-an-allowlist) | 2026-05 | Accepted | A supervisor spawns `claude remote-control` servers from a host-scoped allowlist |
| [GD-0005](#gd-0005--session-title-is-the-only-grouping-handle) | 2026-05 | Accepted | Session `title` is the only writable grouping handle; nickname-prefix it |
| [GD-0006](#gd-0006--shared-app-one-env-pools-code-in-ai-harness-state-per-host) | 2026-05 | Accepted | Shared AppOne env pools: code vendored in ai-harness, runtime state per-host |
| [GD-0007](#gd-0007--single-operator-one-ssh-identity-reused-everywhere) | 2026-05 | Accepted | Single-operator setup: one SSH identity reused across hosts/GitHub/DB |
| [GD-0008](#gd-0008--secrets-stay-out-of-git-as-the-default-posture-not-yet-uniform) | 2026-05 | Accepted | Secrets-out-of-git is the intended default posture (uniform adoption is in progress) |
| [GD-0009](#gd-0009--manager-tracked-worker-roster-shared-into-every-sibling-brief) | 2026-07 | Accepted | Manager keeps a live worker roster and shares it into every sibling worker's brief |

---

## GD-0001 — GitHub Projects (not Jira); one board can span repos

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Work spans several repos but wanted one place to track it without
  hosting/licensing a tracker.
- **Decision:** Use **GitHub Projects** (user-level, owner `youruser`), referenced
  by number. Three boards: **Trading & Fund (#1)** spans `app-two`,
  `app-two-aws`, `app-two_python`, `AppTwoAnalysis`, `AppOne`;
  **Remote Control (#2)** covers `ai-harness`/`claude-remote-control`;
  **Job Search (#3)** covers `job-search` (local-only, no remote). A repo's
  folder name need not match its repo name (`app-two-aws` ↔ `~/dev/app-two-docker`).
- **Consequences:** `gh` needs the `project` token scope
  (`gh auth refresh -s project`). Adding an issue = `gh issue create` then
  `gh project item-add <N> --owner youruser --url <URL>`. The board lags reality —
  before starting a ticket, confirm it isn't already merged
  (`git log origin/main --grep "#N"`).
- **Source:** `~/dev/GITHUB_PROJECTS.md`; each repo's CLAUDE.md "Project tracking".

## GD-0002 — Worktree-per-task; never switch the canonical checkout's branch

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Multiple concurrent agents/sessions operate on the same repos on the
  same machine. The canonical checkout's branch and working tree must stay stable
  (the remote-control supervisor and the symlinked pool scripts assume `main` is
  checked out there).
- **Decision:** Do every task in a **git worktree**, for **every** repo (not just
  AppOne). **Never switch the canonical checkout's branch in place.** Adding
  and committing on the existing branch is fine; checking out a different branch in
  the canonical clone is not.
- **Consequences:** Worktree cleanup is deferred — the "remove worktree" step fails
  while the owning session is live (self-lock); let session-exit reap it, never
  `-f -f` the current worktree. When verifying whether an abandoned worktree holds
  unique work, use `git cherry` (merge-base), not a two-dot diff, to avoid
  false-positive "unmerged" readings.
- **Source:** workspace convention; `close-work` / `resume-work` skills;
  `ai-harness/scripts/pool/README.md` ("works both ways" note).

## GD-0003 — Multi-agent ticket coordination via a claim convention

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Several agents (parallel Claude/Codex sessions, often across note
  + mini) share one board, so two could grab the same ticket, or one could die
  mid-ticket and strand it.
- **Decision:** Coordinate with a **claim** lifecycle: `start-work (claim) → work →
  resume-work (if interrupted) → close-work (deliver / hand off)`. A claim =
  **Status → In Progress** + **assignee** + a structured **claim comment**
  identifying the agent by **worktree + host** (`youruser+<worktree>@<host>`). An
  agent is **disconnected** when its transcript is idle ≥ `AGENT_CLAIM_TTL_SECS`
  (1h) or it ended on an API error / usage limit; then its `STALE`/`DEAD` claim is
  reclaimable.
- **Consequences:** "ALIVE" on a claim means **claim age < TTL**, *not* a running
  session — cross-check `ps`/worktree/transcript/`list-sessions` before deferring
  to it (a dead session can leave an un-expired claim). The shared script
  `agent_claims.sh` defaults `--repo` to ai-harness; pass `--repo youruser/AppOne`
  for AO tickets.
- **Source:** `ai-harness/CLAUDE.md` → *Multi-agent ticket coordination*;
  `ai-harness/skills/start-work/scripts/agent_claims.sh`; `skills/README.md`.

## GD-0004 — Remote-control supervisor driven by an allowlist

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Each repo dir that wants remote control needs a long-running
  `claude remote-control` server, and which dirs are active differs per machine.
- **Decision:** A **supervisor** (ai-harness) reads `active-dirs.txt` every ~30s
  and spawns/SIGTERMs one server per allowlisted basename under `~/dev`. The file
  is **git-shared across machines**, so entries are **host-scoped** with `@<nick>`
  (bare = every host; `name@nick1,nick2` = those hosts). `dev` is the special name
  for the `~/dev` root.
- **Consequences:** Enable/disable a dir by editing the allowlist (or the
  `remote-control-dirs` skill), not by killing processes — the supervisor would
  respawn them. Adding a repo to remote control is one line here.
- **Source:** `ai-harness/active-dirs.txt`; `remote-control-dirs` skill;
  `ai-harness/docs/OPERATIONS.md` → *Activation list*.

## GD-0005 — Session `title` is the only grouping handle

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** The code-sessions API has no folders/groups and `tags` is read-only,
  so cross-repo sessions are hard to tell apart in the app's session list.
- **Decision:** Prefix each session's **title** with a per-repo nickname
  (`[AO] …`, `[CRC] …`) so same-repo chats cluster. Rename via
  `python3 -m remote_control titles set --id <sid> "…"` (in `~/dev/ai-harness`) or
  the `rename-sessions` skill.
- **Consequences:** Titles are the durable grouping mechanism; keep the nickname
  prefix stable per repo (`session-nicknames.txt`).
- **Source:** `ai-harness/session-nicknames.txt`; `rename-sessions` skill.

## GD-0006 — Shared AppOne env pools: code in ai-harness, state per-host

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Concurrent sessions need browsable preview envs and isolated test
  DBs without each spinning up a heavy ad-hoc Docker stack, and without clobbering
  the shared dev DB.
- **Decision:** Two **lease-table-coordinated** pools, code **vendored in
  ai-harness** (`scripts/pool/`, symlinked into `~/.app-one-pool/` by `link.sh`)
  with all **runtime state per-host** in `~/.app-one-pool/` (never committed):
  - **preview pool** (`pool.sh`, skill `lease-env`) — shares the dev DB; for
    browsing a branch. **Never run tests here** (RefreshDatabase would wipe dev).
  - **test pool** (`testpool.sh`, skill `test-env`) — slot-private `app-one_testN`
    DBs rebuilt fresh per claim; for RefreshDatabase/coverage/Dusk/parallel runs.
  Slot counts/ports drift (the test pool recently grew to test0..test9) — treat the
  scripts + `scripts/pool/README.md` table as source of truth, not any hardcoded
  number.
- **Consequences:** Editing the *live* `~/.app-one-pool/*.sh` path edits this
  repo's working tree (it's a symlink) on whatever branch is checked out (usually
  `main`) — such edits are uncommitted until committed here; commit-with-attribution
  rather than revert an unexpected dirty `scripts/pool/*.sh`. `gc` reclaims a slot
  only when its worktree/container is gone, not for a still-leased idle agent
  (zombie-lease sweep is the `resume-work` skill). Scripts hardcode
  `/Users/user/dev/AppOne` and the `user` home — porting to another
  host/user needs those parameterized.
- **Source:** `ai-harness/scripts/pool/README.md`; `lease-env` / `test-env` skills;
  AppOne `CLAUDE.md` (Testing → testpool).

## GD-0007 — Single-operator: one SSH identity reused everywhere

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** Single operator across several machines + internal hosts; no
  team to provision per-user keys for.
- **Decision:** Reuse **one `~/.ssh` bundle** everywhere — internal LAN hosts
  (e.g. a NAS that holds DB dumps and the age/secrets bundle, a prod deploy
  target), GitHub, and the DB hosts. New machine = copy `~/.ssh` from a
  working one (+ `/etc/hosts` entries for any LAN hostnames), not fresh
  keygen. Note: some appliances (e.g. Synology) drop the SFTP subsystem, so
  pull with `scp -O` rather than `sftp`.
- **Consequences:** Compromise of the bundle is broad-blast-radius (accepted
  for a single-operator LAN setup). Prod is never edited directly — edit
  locally, deploy.
- **Source:** per-repo `SETUP.md` / `CLAUDE.md` (Commands → deploy/prod).

## GD-0008 — Secrets stay out of git as the default posture (not yet uniform)

- **Date:** 2026-05 · **Status:** Accepted
- **Context:** A public `.env` leak (AppOne) drove a secrets-management
  posture. The same posture should apply to the other repos, but adoption is uneven.
- **Decision:** The **intended default** across repos: real secrets in **git-ignored**
  files (only `.env.example` tracked), **rotation** on leak + periodically, and
  **secret scanning** (gitleaks in git hooks + CI) — *not* a secrets server. Where
  it's worth versioning secrets, encrypt them in-repo with **SOPS + age**
  (committed `*.sops` twins, private age key off-repo). AppOne is the reference
  implementation (its `ED-0001`/`ED-0002`).
- **Consequences:** This is **aspirational, not uniform** — e.g. `app-two-aws`
  still has weak/hardcoded prod creds (analyze-only, out of AppOne's scope).
  Promote a repo by porting AppOne's `bin/secrets.sh` + git-hooks + CI secret
  scan. Track per-repo adoption rather than assuming it.
- **Source:** AppOne `ED-0001`/`ED-0002`, `docs/security/`; this workspace's
  secret-rotation history.

## GD-0009 — Manager-tracked worker roster, shared into every sibling brief

- **Date:** 2026-07 · **Status:** Accepted
- **Context:** A manager spawning several workers toward one goal (per
  [[manage]]) previously gave each worker ONLY its own brief. Workers had no
  visibility into sibling workers' scope or decisions already made, so
  parallel workers could duplicate work or contradict each other (e.g. two
  workers computing the same ratio two different ways).
- **Decision:** The manager keeps a live **worker roster** (this session's
  todo list, one todo per live worker — subname, `cse_*`, one-line
  responsibility — backed by the durable `workers.sh` state log) and
  transcribes it into every new worker's brief as a mandatory "Sibling
  workers" section, plus a "Settled decisions" section for anything a
  sibling already decided that the new worker must not recompute. Mid-flight
  `send-to-session` pushes into an existing worker attribute the source
  sibling and say whether it supersedes an assumption. Workers report
  "state of my work" + assumptions siblings might depend on when they
  report back, and stop + report (rather than silently resolve) any overlap
  with a sibling's listed scope.
- **Consequences:** Every full-session worker brief now carries roster
  bookkeeping overhead (the manager must keep the todo list current); a
  stale roster produces a stale "Sibling workers" section, so the manager
  must update it on every dispatch/close/forget, not just at dispatch time.
- **Source:** `ai-harness/skills/manage/SKILL.md` ("Worker roster" section);
  `ai-harness/skills/new-session/brief-template.md`;
  `ai-harness/skills/new-session/SKILL.md` ("Multi-worker dispatches").
