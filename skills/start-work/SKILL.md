---
name: start-work
description: >-
  Start work on a tracked ticket as one of several parallel agents: pick a Todo
  item off the GitHub Project board, verify it isn't already held by a still-
  alive agent, then CLAIM it — post a structured claim comment (agent identity:
  worktree + host), assign it, and move it to In Progress — so concurrent agents
  on other sessions/machines don't double-work the same ticket. Use when the
  user says "start work on #N", "pick up a ticket", "claim this issue", "grab
  the next Todo", "what's free to work on", or "who's working what right now".
---

# Start work on a ticket (claim it for this agent)

Several agents (different Claude/Codex sessions, often on different machines —
note / mini) share one GitHub Project board. This skill is how an agent
**picks and claims** a ticket so the others can see it's taken and by whom. It
is the front half of the lifecycle: `start-work` → (work) → [[resume-work-skill]]
if interrupted → [[close-work-skill]] to deliver.

The claim is **hybrid / GitHub-anchored** (one shared source of truth, syncs
across machines for free):

- **Status → In Progress** — the canonical "being worked" signal.
- **Assignee → `youruser`** — coarse "claimed by an agent" board filter (we
  only have one GitHub account, so the assignee can't say *which* agent).
- **A structured claim comment** carries the agent identity, anchored on
  **worktree path + host** (the reliable join key — the readable `cse_` id lives
  only in the worktree path, while the transcript filename is an unrelated UUID).

See [[gh-projects-tracking]] (mapping + commands in `~/dev/GITHUB_PROJECTS.md`)
for which GitHub Project board covers which repo in your setup.

## Tool

`scripts/agent_claims.sh` — the shared claim/liveness helper (also used by
`resume-work` and `close-work`). Resolves `gh`/`jq` even off-PATH.

- **`agent_claims.sh whoami`** — this agent's identity token + worktree, branch,
  host, and its transcript freshness. Read-only; run it first to confirm who you
  are. Token format: `youruser+<worktree-basename>@<host>`.
- **`agent_claims.sh check <issue#> [--repo O/R]`** — read the latest claim
  comment and print a liveness verdict (read-only): `ALIVE` (active <1h, **do
  not take**) · `STALE` (≥1h idle, reclaimable) · `DEAD` (ended on API error /
  usage limit) · `OFFHOST` (claimed on another machine — verify there) ·
  `RELEASED` / `NOCLAIM` (free).
- **`agent_claims.sh todo [--project N | --all-projects] [--owner O]`** — every
  **available** (Status=Todo) ticket across the boards, grouped by board, tagged
  with repo and the issue's GitHub labels in `[brackets]` (and `(draft)` for
  draft notes). Sweeps all boards by default; the read-only "what's free to pick
  up" view (see [[list-tickets-skill]]).
- **`agent_claims.sh list [--project N | --all-projects] [--owner O]`** — every
  In-Progress ticket on the board(s) with its claim + liveness verdict. The
  "who's working what, and are they still alive" dashboard.
- **`agent_claims.sh claim <issue#> [--repo O/R] [--project N]`** — post the
  claim comment, add the assignee, and move the item to In Progress. **Mutates
  the board** — confirm with the user first (status edits need the `project`
  token scope; it degrades to a printed manual fallback if absent).

Liveness is **passive**: an agent is alive if its session transcript was
appended within `AGENT_CLAIM_TTL_SECS` (default 3600 = **1h**) and didn't end on
an API error / usage limit. No cooperation needed from the working agent.

## Workflow

1. **Know yourself.** Run `agent_claims.sh whoami` and confirm the worktree +
   host look right (you should be in the worktree you'll do the work in).
2. **Pick a ticket — let the user choose.** Show open Todo items and **never
   auto-pick**. Easiest is the cross-board view (also the [[list-tickets-skill]]):
   ```bash
   scripts/agent_claims.sh todo                 # available Todo across all boards
   scripts/agent_claims.sh todo --project <N>   # just one board
   ```
   or straight from `gh`:
   ```bash
   gh issue list --repo <owner>/<repo> --state open
   gh issue list --repo <owner>/<repo> --state open --label <label>  # filter by repo's label taxonomy if it has one (see Labels below)
   gh project item-list <N> --owner <owner>     # to see Status per item
   ```
   If the user already named an issue #, skip to step 3 with it.
3. **Check it isn't already taken by a live agent** — `agent_claims.sh check
   <issue#>`:
   - **ALIVE** → stop. Another agent is actively on it; point the user at that
     session/host instead of duplicating (this is the [[feedback_parallel_sessions]]
     rule — resume/redirect, don't rebuild). Offer `resume-work` if they want to
     take over deliberately.
   - **STALE / DEAD / RELEASED / NOCLAIM** → free to take. (STALE/DEAD means a
     prior agent disconnected — you're reclaiming it.)
   - **OFFHOST** → it's claimed on another machine and can't be verified here.
     Surface that and let the user decide (check that machine, or take it).
4. **Claim it (confirm first).** With the user's OK:
   `agent_claims.sh claim <issue#> --project <N>`. This posts the claim comment,
   assigns `youruser`, and sets Status → In Progress. If the status edit prints
   a manual fallback (no `project` scope), set it in the board UI or run
   `gh auth refresh -s project --hostname github.com` once.
5. **Reflect the ticket in this session's title.** Right after a successful
   claim, retitle *this* chat so the app's session list shows what each agent is
   on:
   ```bash
   python3 -m remote_control titles set --self "#<issue#> <short ticket title>"
   ```
   `--self` derives this session's id and the repo nickname from the worktree, so
   the title becomes `[NICK] #<issue#> <short ticket title>` (idempotent — safe to
   re-run as the focus sharpens). This is part of the claim action — no extra
   confirmation needed. See [[rename-sessions-skill]] for the underlying tool.
   The platform's auto-titling may later overwrite this and strip the `[NICK]`
   prefix; the usage-limit monitor re-applies prefixes every
   `SESSION_TITLE_APPLY_SECS` (default 600s), so drift self-heals — no need to
   re-run `titles set` yourself.
6. **Do the work** in this worktree. If the session is interrupted, the claim's
   transcript will go stale after 1h and the ticket becomes reclaimable by
   `resume-work`; when you finish, `close-work` releases the claim and closes
   the ticket.

## Labels (per-repo label taxonomy)

Some repos carry a **domain + meta** label taxonomy on their issues — use it
to *filter* when browsing, and **apply it when you file or claim** a ticket
that's missing labels. List the live set with:

```bash
gh label list --repo <owner>/<repo>
```

A typical taxonomy:

- **Domain** (pick the most specific that fit): `security` · `ci`
  (pipeline / scan gates) · `infra` (repo visibility / secret vaulting) ·
  `acl` (authz / access control) · `ui` · `ops` (prod deploy / backfill) ·
  `tooling`. Features use the built-in `enhancement`.
- **Meta:** `epic` (umbrella / tracking issue with child tickets) · `blocked`
  (unmet hard dependency or decision gate — **keep this accurate** as deps clear).

Filter the board by label when picking work, e.g.:

```bash
gh issue list --repo <owner>/<repo> --label ci                  # pipeline tickets
gh issue list --repo <owner>/<repo> --label blocked             # what's gated
gh issue list --repo <owner>/<repo> --label security --label acl
```

Repos without a custom taxonomy use the **default** GitHub label set.

## Rules

- **Never auto-pick a ticket** — the user chooses (step 2).
- **Keep labels accurate** (for repos with a custom taxonomy): give a
  newly-filed/claimed ticket its domain label(s), and clear `blocked` once
  its dependency lands (see Labels).
- **Never claim a ticket that `check` reports ALIVE** without the user explicitly
  deciding to take over a live agent — that's the double-work this skill exists
  to prevent.
- **Confirm before mutating the board** (claim posts a comment + assignee +
  status). A "start work" request is not blanket approval to edit tickets.
- One ticket → one agent. If you need to split work, file/claim separate issues.
- Cross-host claims are best-effort: liveness can only be verified on the
  claiming machine.
