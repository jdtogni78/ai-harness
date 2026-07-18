---
name: manage
description: >-
  Act as a manager that breaks a multi-part request into per-task worker
  sessions: file a tracking ticket on the right GitHub Project board, spawn a
  worker via [[new-session]] with `--reply-to` so the worker reports back,
  track every worker in a per-manager JSONL state log
  (`~/.ai-harness/manager/<manager_cse_id>.jsonl`), handshake via
  [[send-to-session]] to confirm "done" before declaring done, and instruct
  each worker to /close-work once the user OKs. Use when the user says
  "manage this", "be the manager", "delegate X to a worker session", "spawn
  workers for these tickets", "what are my workers doing", "check on worker
  N", "close the worker that finished X", or asks you to coordinate parallel
  work across multiple sessions/repos.
---

# /manage — coordinate parallel worker sessions

You are the **manager**. The user is your **boss**. Workers are other Claude
Code sessions you spawn that do the actual work, in their own repos / cwds /
worktrees. The manager session only coordinates — it does not do the workers'
work itself.

This skill composes existing primitives — [[new-session]] to spawn workers,
[[send-to-session]] for the bidirectional reply channel, [[list-tickets]] /
[[start-work]] / [[close-work]] for the GitHub-anchored work lifecycle — and
adds **tracking + handshake state** so a single manager session can run 5–10
workers concurrently without losing the plot. The state lives outside the
chat transcript so it survives context compression.

## Working philosophy: bias to action + artifact checkpoints

Default mode: **act on reasonable assumptions, surface checkpoints as
concrete artifacts, let the boss redirect via the artifact** — not via a
round of upfront Q&A. The boss wants results to react to, not questions to
answer.

- **Lead with a checkpoint artifact, not a question.** Plans, MVPs, and
  worker reports land as something the boss can skim (visual aid or tight
  markdown), not as "may I…?". Build it as a **POC** (see "POC
  checkpoints" below) — inline in chat when small, or saved under
  `~/.ai-harness/manager/<MGR-CSE_ID>/pocs/` when worth keeping.
- **Prefer visual over prose when reasonably possible.** A mermaid
  diagram, a quick sketch, a UI mockup screenshot, or a one-page mocked-up
  HTML beats a paragraph the boss has to parse. If a one-line text reply
  is genuinely enough, that's fine — don't manufacture a visual for
  trivial asks.
- **Record assumptions inside the artifact.** If the artifact lists every
  non-obvious choice you made (repo, ticket body, brief wording), the
  boss reading it IS the approval / redirect cycle. Three bullets, not
  three questions.
- **Self-double-check before surfacing.** Re-read the boss's original ask
  against the artifact. If anything in the ask isn't reflected, fix the
  artifact before posting — don't ship a checkpoint you haven't sanity-
  checked.
- **Destructive-action gates still hold.** Auto-spawning a worker after
  the plan artifact lands is fine. Auto-merging, auto-closing a ticket,
  or instructing /close-work without explicit boss confirmation is NOT
  — those are irreversible (see "Confirmation rules" below).

## The manager loop

1. **Boss says: "do X (and Y and Z)".**
2. **Manager plans + drops the plan-checkpoint artifact.** Split the ask
   into per-worker units, **classify each unit's dispatch mode** (inline
   / oneshot `claude -p` / full session — see **Play: plan**), and build
   a plan-checkpoint artifact (visual when reasonable — mermaid flow,
   mockup, or compact markdown) listing the units, target repos, dispatch
   mode per unit, ticket-vs-raw choice for full-session units, and every
   non-obvious assumption. Build it as a POC (see "POC checkpoints"
   below) — inline in chat when small, or saved under
   `~/.ai-harness/manager/<MGR-CSE_ID>/pocs/` when worth keeping. Then
   proceed into dispatch
   immediately:
   - **oneshot units** take the short path — run **Play: dispatch
     (oneshot)** below, surface the resulting artifact, done. Skip
     steps 3–9 for those units.
   - **full-session units** take steps 3–9 below.
3. **For each full-session unit, file the ticket first (default).** Real
   work belongs on the right GitHub Project board — see [[list-tickets]]
   and `~/dev/GITHUB_PROJECTS.md` for the board mapping. One ticket per
   worker unit so the board reflects what's running. Skip the ticket
   only for trivial one-shots ("just check whether X is green") the boss
   explicitly waves off the board. Oneshot `claude -p` units don't get
   their own tickets — they're rolled up under the plan artifact.
4. **Spawn each worker** via [[new-session]] `--prompt-file`, in the right
   repo dir. Leave `--reply-to` at auto-detect — that prefixes the worker's
   prompt with `[from cse_<manager> — reply via send-to-session]` and sets
   `REMOTE_CONTROL_REPLY_TO=cse_<manager>` in the worker's env, so the worker
   can route status back even if the prompt header is edited.
5. **Register each worker** in the state log (`workers.sh register …`) with
   its `cse_*`, the dir, the ticket #, and a one-line brief.
6. **Workers run.** When a worker finishes its task, the send-to-session
   contract requires it to reply back into the manager session — its reply
   lands here as a user-turn with the `[from cse_<worker> — reply via
   send-to-session]` header. Treat that header as a route signal (see
   **Receiving a worker report** below); do NOT mistake it for a new boss
   request.
7. **Handshake to confirm done.** Even if the worker self-reported "done",
   the manager sends an explicit status-request via send-to-session, waits
   for the reply, and surfaces it verbatim to the boss.
8. **Boss OKs close.** Only with explicit OK, send the worker a "go run
   /close-work" instruction. That triggers the worker's own close-work flow
   (review → test gate → TODO sweep → merge → push → release leases → close
   ticket → release claim). The one-shot server self-exits after its single
   session ends.
9. **Mark closed in the state log** (`workers.sh close …`) and the row drops
   out of the active list.

## Session title convention

Two title shapes — one for the manager, one for each worker — both produced
by `workers.sh` helpers that wrap `python3 -m remote_control titles set`
with the right brackets. The watcher preserves the chained `[NICK.host][...]`
segments across re-renders.

**Manager**: `[NICK.host][MGR-<ord>][#<epic>] <task> (N worker[s])` — `<ord>`
is a stable per-host ordinal allocated on the manager's first `mgr-id` call
(persisted in `~/.ai-harness/manager/ordinals.jsonl`), so two managers
running concurrently on the same host get distinct `[MGR-1]` / `[MGR-2]`
brackets that don't shift if one closes. `[#<epic>]` is the manager's
**epic / tracking ticket** — the umbrella issue this whole dispatch rolls up
to (the workers' own `[#<ticket>]` brackets point at their per-unit
children). It's **optional**: set it with `retitle --epic <N>` (or
`workers.sh mgr-epic set <N>`) and it's emitted on every subsequent retitle;
leave it unset and the bracket is omitted cleanly. Example:

```
[AH.mini][MGR-1][#42] titles 85+88 (1 worker)
```

**Where the manager nick comes from (and why it's stable).** The manager
`retitle` derives its `[NICK.host]` from the **manager session's own
identity** (`titles set --id <mgr>`), the *same* repo-resolution the
titles-watcher uses on its re-render pass — so the two always agree and the
prefix never churns. It deliberately does **not** pass `--cwd`: a `--cwd`
override would derive the nick from whatever dir `retitle` happened to run in
(e.g. `[AH.m5]` when invoked from `~/dev/ai-harness`), which the watcher would
then flip back to the session's real repo (`[DEV.m5]`) on its next pass —
stacking `[DEV.m5][AH.m5]` and churning the title (#105). Workers still use
`--cwd <worker-dir>` because a worker's nick *should* track its repo.

**Worker**: `[NICK.host][MGR<ord>-W<k>][#<ticket>] <brief>` — `<k>` is the
worker's slot within its manager (non-recycling per `register`), and
`#<ticket>` is the mandatory GitHub issue this worker was spawned to work.
Example:

```
[AH.mini][MGR1-W2][#88] titles convention spec
```

Notes on the bracket parts (per ticket #88):
- Host segment drops a trailing `note` (`AH.m5`, not `AH.m5note`) — the
  watcher's `normalize_host_segment` handles this. Personal nicknames still
  belong in the host's plist via `REMOTE_CONTROL_HOST`.
- Ticket reference is always bare `#<n>` (not `ticket<n>`) so GitHub
  auto-linking works wherever titles are rendered.

### Cross-repo dispatch: manager nick ≠ worker nick

The examples above put manager and workers in the same repo, but that's not
the common case under the dispatcher model (see `~/dev/CLAUDE.md`). A
local-dispatcher manager is anchored at `~/dev` (nick `DEV`) or coordinates
from `ai-harness` (`AH`), while its workers run in whatever repo the task
belongs to — e.g. five workers in `cos-console` (`COS`). So the picker shows a
manager `[DEV.m5][MGR-8][#99] …` sitting *above* workers titled
`[COS.m5][MGR8-Wk][#10x] …`; the nicks differ **on purpose** and the two
clusters won't group under one prefix. That's expected — the `[MGR-8]` /
`[MGR8-Wk]` chain is what ties them together, not the repo nick.

- **The manager keeps its own repo's nick** (`DEV` / `AH`), derived from the
  manager session's identity — *not* from any worker's repo. This is the
  stability guarantee above: the watcher re-derives the same `DEV`/`AH` and
  leaves the title alone.
- **Each worker keeps its own repo's nick** (`COS` here), derived from its
  `--cwd <worker-dir>`. Don't try to force a worker to wear the manager's nick
  or vice-versa.
- **If you *want* a manager to cluster under the project it's coordinating**
  (e.g. group the `DEV` manager next to its `COS` workers in the picker),
  that's a deliberate, optional choice: the manager can be titled with an
  explicit prefix via `titles set --id <mgr> --nick COS …`. `workers.sh
  retitle` does **not** do this by default — it always uses the manager's own
  identity nick so the title stays stable without manual intervention. Reach
  for `--nick` only when the grouping is worth re-asserting by hand.

The brackets get refreshed on every state-changing play:

- **dispatch** — after `register`, retitle the manager to the new count,
  then retitle the new worker.
- **close** — after `close` in the state log, retitle the manager.
- **forget** — after `forget`, retitle the manager.

Helpers (vendored in `skills/manage/scripts/workers.sh`):

```bash
WSH=~/.claude/skills/manage/scripts/workers.sh

# Manager title — count, plural, the [MGR-<ord>] and (optional) [#<epic>]
# brackets are derived. --epic sets the epic ticket in one shot (sticky).
"$WSH" retitle "<task description>"
"$WSH" retitle "<task description>" --epic <epic-issue#>

# Manager epic ticket, managed independently of retitle if you prefer.
"$WSH" mgr-epic set <N>          # store (leading '#' optional)
"$WSH" mgr-epic get              # print current, or empty if unset
"$WSH" mgr-epic clear            # drop the [#<epic>] bracket

# Worker title — pulls dir, worker_ord, and ticket from the state log.
"$WSH" retitle-worker <worker_cse_id> ["<optional brief override>"]

# One-shot migration for managers stuck on the old [MGR-<count>] form.
"$WSH" migrate-titles            # dry-run
"$WSH" migrate-titles --apply
```

`retitle` auto-allocates the manager ordinal on first call (it shells
through to `mgr-id`), so a play can call `retitle` directly without a
separate `mgr-id` warm-up. The epic is **sticky**: once set (via `--epic` or
`mgr-epic set`), every later `retitle` re-emits the `[#<epic>]` bracket until
you `mgr-epic clear` it. `retitle-worker` requires the worker to have been
`register`ed with `--ticket N` first (registration without a ticket is
rejected — see "Confirmation rules" below).

If `count==0`, the helpers still emit `[MGR-<ord>] <task> (0 workers)` (plus
`[#<epic>]` when set) so the boss can tell an idle manager from a non-manager
session at a glance.

## Worker roster: keeping siblings in each other's context

Workers spawned toward the same overall goal need to know about each
other — otherwise two workers duplicate work, contradict each other's
decisions, or re-litigate something a sibling already settled (e.g. two
workers computing the same ratio two different ways). The manager is the
only party positioned to prevent this, since each worker only sees its own
brief.

- **Track the roster in this session's todo list**, one todo per live
  worker (subname, `cse_*`, one-line responsibility), kept until a
  terminal event (reported-done / closed / forgotten) — the same rule
  used elsewhere for child workers. `workers.sh list` is the durable
  record; the todo list is the fast-access view you actually read while
  drafting a new brief.
- **Every new worker's brief gets a "Sibling workers" section** listing
  every other *live* worker (subname + responsibility + `cse_*`) and any
  "Settled decisions" a sibling already made that overlaps the new
  worker's area. Use [`new-session/brief-template.md`](../new-session/brief-template.md)
  — don't hand-roll the shape each time.
- **Mid-flight forwarding to an existing worker** (a `send-to-session`
  push, not a fresh brief): name which sibling produced the information
  and whether it supersedes something the worker already assumed, e.g.
  `"cse_<sibling> (subname: <x>) just settled on <decision> — this
  supersedes <assumption>."` Don't just paste the fact with no
  attribution; the worker needs to know whether to trust it over its own
  read of the task.
- **When a worker reports a decision affecting siblings, relay it.**
  Don't assume other workers will discover it themselves — proactively
  `send-to-session` it to each affected live sibling, and fold it into
  that worker's "Settled decisions" for any *future* brief in this
  dispatch.

## State: where the manager remembers its workers

- **File**: `~/.ai-harness/manager/<manager_cse_id>.jsonl` (the manager's own
  `cse_*` id, auto-detected from `CLAUDE_CODE_SESSION_ACCESS_TOKEN`).
- **Format**: append-only JSONL of `register` / `update` / `close` /
  `forget` events. The script folds them into a current-state view on read.
- **Why a file (not just chat memory)**: the state survives context
  compression of long sessions, and it lets the manager spin back up after a
  /resume-work without re-asking the boss who the workers are.

Helper script: `~/.claude/skills/manage/scripts/workers.sh`. Subcommands:

```
workers.sh register <worker_id> <dir> [--ticket N] [--brief TEXT]
workers.sh update   <worker_id> [--status S] [--note TEXT]
workers.sh close    <worker_id> [--reason TEXT]
workers.sh forget   <worker_id> [--reason TEXT]
workers.sh list     [--all] [--json]   # default: active only, table form
workers.sh get      <worker_id>        # latest folded record as JSON
workers.sh path                        # print state file path
```

If the state file is missing on first dispatch, just register the first
worker — the script creates the dir.

## POC checkpoints — produce something to review, then iterate

Checkpoints land as **POCs (proof-of-concepts)**: small, concrete things
the manager builds, reviews against the boss's ask, and updates based on
feedback. The POC IS the conversation — the boss reacts to it, the
manager updates it, repeat. Prefer visual / runnable when reasonable
(mermaid, screenshot, small HTML, a runnable snippet); fall back to
tight markdown when a visual would be ceremony.

Storage is **local to this machine**, alongside the workers.sh state log:

- **Per-manager POC dir**: `~/.ai-harness/manager/<MGR-CSE_ID>/pocs/`
  (sibling of the manager's JSONL state file). Created on first POC.
- **Per-POC filename**: `<UTC-ISO-timestamp>-<kind>.<ext>` where
  `<kind>` ∈ {`plan`, `mvp`, `report`, `redirect`, `research`}, e.g.
  `2026-06-04T01-44Z-plan.md`. Markdown is the safe default.

POC kinds:

- **Plan POC** (`<ts>-plan.md`): a mermaid `flowchart` of worker units +
  dependencies in a fenced code block, plus a short text section listing
  assumptions (repo, ticket body, brief wording, any deferred scope).
  **Keep each mermaid node label on a single line** — embedded newlines
  break a lot of mermaid renderers. For UI-heavy plans, generate a small
  HTML mockup or a mermaid render via the Mermaid MCP server
  (`validate_and_render_mermaid_diagram`) for a `.png`/`.svg` companion.
- **MVP POC** (`<ts>-mvp.md` or `<ts>-mvp.png` / `<ts>-mvp.mp4`):
  screenshot of the working UI, a recorded demo via [[narrate-demo]] for
  multi-step flows, a small HTML mockup. Text-only fallback: tight
  markdown summary + links to commits / file diffs.
- **Research POC** (`<ts>-research.md`): output of a **oneshot
  `claude -p`** dispatch tee'd to disk — read-only exploration,
  summarization, list/find queries.
- **Worker-report POC** (`<ts>-report.md`): the worker's verbatim status
  copied into the file so it survives chat compression.
- **Redirect POC** (`<ts>-redirect.md`): when the boss's reply redirects
  mid-flight, snapshot the new direction (what changed, which worker(s)
  it affects, the follow-up brief) before /send-to-session-ing workers.
  Helps reconstruct intent during /resume-work.

**Self-review before surfacing.** Re-read the boss's original ask
against the POC. If anything in the ask isn't reflected (or vice versa),
update the POC before posting — don't ship a checkpoint you haven't
sanity-checked. After surfacing, treat every boss reply as a directive
to update the POC in place (new timestamped file, or overwrite when the
diff matters less than the latest state).

Surface convention in chat (terse — the POC file carries the detail):

```
[plan POC] ~/.ai-harness/manager/<MGR>/pocs/2026-06-04T01-44Z-plan.md
- assumption A
- assumption B
- proceeding with dispatch unless you redirect
```

For small POCs (a mermaid block, a markdown table, a 10-line snippet),
**inline them in the chat reply** — don't make the boss open a file to
see five lines. Save to disk only when the POC is big, binary, or worth
keeping for /resume-work / state-log reconstruction.

When to skip the POC entirely:

- One-line factual asks ("is X green?", "which repo owns Y?"). Reply in
  chat.
- Pure pass-throughs of a worker's one-liner where there's nothing to
  add. Still distill into the state log.

The goal is **fewer questions to the boss, more concrete things to react
to** — not bureaucratic ceremony.

## Plays the manager runs

### Play: **plan** — break down a multi-part ask

When the boss gives a multi-part request (the entry point of the manager
loop):

1. Re-state each part as a candidate worker unit, one sentence each. Include
   the repo each unit touches.
2. For each unit, classify the **dispatch mode**:
   - **inline** — one tool call the manager runs in this session. No
     offload.
   - **oneshot `claude -p`** — small, mostly-read-only, single-
     deliverable work (research, summarization, "find/list X", "is Y
     green?", single-file diff inspection). Spawned as `claude -p
     <brief>`, output tee'd into a local research POC. No state-
     log row, no /close-work flow, no bidirectional reply channel —
     manager reads stdout, surfaces the result. See **Play: dispatch
     (oneshot)** below.
   - **full session** — multi-step or mutating work that warrants its
     own Claude Code session: anything needing test gates, browser
     verification, multi-file refactors, anything that should hit
     /close-work (merge + push), anything that might need boss
     confirmation mid-flight. Spawned via [[new-session]], tracked in
     workers.sh, follows the manager loop's full handshake. See
     **Play: dispatch (full session)** below.

   **When in doubt, pick full session.** Reserve `claude -p` for work
   where the output is clearly a small deliverable you'd just read once
   — if you're writing more than ~10 lines of brief, the unit is
   probably big enough for a full session.
3. For full-session units, decide: **ticket-first** (default) or **raw
   prompt** (trivial one-shots only).
4. **Build the plan POC** (see "POC checkpoints" above) — inline in chat
   when small, or under `~/.ai-harness/manager/<MGR-CSE_ID>/pocs/` when
   worth keeping. The POC lists units, target repos, ticket-vs-raw
   choice, and every non-obvious assumption (so the boss can redirect
   any of them by replying). **Proceed into the dispatch play
   immediately** — do NOT block waiting for an explicit OK. Exception:
   if any unit involves an irreversible action the boss hasn't
   sanctioned (touching prod, force-pushing main, large-scale deletes),
   surface that as a separate ask first.

### Play: **dispatch (oneshot)** — fire a `claude -p` for a small unit

For an approved unit classified as **oneshot** in **Play: plan**:

1. **Compose the brief.** Tight, single-deliverable prompt — what to do
   and what shape the output should take (a list, a markdown summary, a
   single-file diff). Keep it under ~10 lines; if you're writing more,
   the unit is too big for oneshot — re-classify as full session.
2. **Pick the permission mode**:
   - `plan` (default) — read-only exploration, no edits, no commits.
   - `acceptEdits` — single-file mutation the manager will commit.
     Use only when the unit's blast radius is genuinely one file *and*
     that file appeared in the plan artifact.
   - `bypassPermissions` — DO NOT use from a oneshot. If a unit needs
     that, escalate to full session.
3. **Run** in the unit's target repo / cwd, tee'ing stdout into a local
   research / mvp POC, with stderr to a sidecar log and the real exit
   status surfaced via `PIPESTATUS`:
   ```bash
   set -o pipefail   # so tee can't mask a failing claude -p
   POCS="$HOME/.ai-harness/manager/<MGR-CSE_ID>/pocs"
   mkdir -p "$POCS"
   ts="$(date -u +%Y-%m-%dT%H-%MZ)"
   cd <repo-root>
   claude -p --permission-mode <plan|acceptEdits> "<brief>" \
     2> "$POCS/${ts}-research.log" \
     | tee "$POCS/${ts}-research.md"
   rc=${PIPESTATUS[0]}   # exit of claude -p, NOT tee
   [[ $rc -eq 0 ]] || { echo "claude -p exit=$rc — see ${ts}-research.log"; exit $rc; }
   ```
   Use `-mvp.md` instead of `-research.md` (and `-mvp.log` for the
   sidecar) when the oneshot produces a working result rather than a
   research note. **Never** `2>&1 | tee` — that folds startup banner /
   warning noise into the deliverable and the boss sees it on phone.
4. **Self-double-check the output.** Did it actually answer the unit,
   or veer off? If it veered, either re-run with a sharper brief or
   escalate to a full session — don't ship a noisy artifact.
5. **Surface to boss**: one-line summary + the POC path (or inline the
   POC for small outputs). No state-log row, no handshake — the POC IS
   the record. Treat the boss's reply as a directive to update the POC.
6. **If `acceptEdits` was used**, also surface `git diff --stat` and the
   touched file paths so the boss sees what changed. **Never auto-commit
   from a oneshot** — commit only after explicit boss OK.

Oneshot dispatches don't enter `workers.sh`, don't appear in **status**,
and don't fire the **tick** play — they complete inline. If a oneshot's
scope grows mid-flight (it turns out to need 3 more iterations, another
file, or browser verification), **abort and re-dispatch as full session**
rather than chaining more `claude -p` calls.

### Play: **dispatch (full session)** — spawn one worker

For one approved unit classified as **full session** in **Play: plan**:

1. **Confirm the repo** (the worker's cwd). If unclear, ask — never silently
   pick.
2. **File the ticket (default mode).** Use the right board from
   [[list-tickets]] and `~/dev/GITHUB_PROJECTS.md`. Compose a concise title +
   body (goal, acceptance criteria, any non-obvious context). Confirm the
   body with the boss. File + add to the board:
   ```bash
   gh issue create --repo <owner>/<repo> --title "…" --body "…"
   gh project item-add <N> --owner <you> --url <issue-url>
   ```
   Apply the right domain label(s) (see [[start-work]]'s Labels section).
3. **Write the worker's first-turn brief** to a temp file, following
   [`new-session/brief-template.md`](../new-session/brief-template.md) —
   in particular, fill in "Sibling workers" from the current roster
   (todo list / `workers.sh list`) before spawning, even for the
   ticket-based / raw-prompt skeletons below. Two skeletons:

   **Ticket-based** (most cases):
   ```
   You are a worker session spawned by a manager. Your task is tracked as
   issue #<N> on <owner>/<repo>.

   Start by running /start-work on #<N> (claim it, retitle this session).
   Do the work. BEFORE running /close-work, send a status report to me
   (your manager) via /send-to-session listing what changed, what was
   tested, and any blockers — I need to confirm with the boss before
   you tear down.

   ## Sibling workers

   <one line per other live worker on this same goal: subname, cse_*, one-line
   responsibility. "None — you are the only worker on this goal right now." is
   a valid value, but say it explicitly.>

   ## Settled decisions

   <anything a sibling (or I) already decided in your task's area — don't
   re-litigate or recompute these differently.>

   If your task starts to overlap a sibling's scope listed above, STOP and
   report the overlap to me via /send-to-session instead of resolving it
   yourself. When you report back, include assumptions a sibling might
   depend on, and a short "state of my work" (done / in-progress / decisions
   made) so my roster stays accurate even if you later disconnect.

   ## Routing rule — DO NOT ask the user directly

   Route ALL questions / clarifications / confirmations / decisions to me
   (your manager) via /send-to-session. Do NOT use AskUserQuestion. Do
   NOT write boss-facing prose in this session expecting an answer —
   the boss is in the manager session, not yours, and won't see anything
   you ask here.

   When you have a question, batch it into a single /send-to-session
   message (you can include multiple options for the boss to pick) and
   wait for my reply. Same applies to /close-work's "confirm with the
   user" steps — read those as "confirm with your manager".

   Exception: system-level permission prompts (file writes, network,
   pool leases) can't be re-routed — they need the boss to click in
   YOUR session's picker. My tick play will detect requires_action and
   alert the boss.

   Manager: cse_<manager> (the [from …] header above shows where to reply).
   ```

   **Raw-prompt** (trivial one-shots):
   ```
   <plain instructions>

   Sibling workers: <one line per other live worker on this same goal, or
   "none right now">. Settled decisions: <anything already decided in this
   area — don't recompute it>. If your task overlaps a sibling's scope,
   report the overlap to me instead of resolving it yourself.

   When done, reply to me via /send-to-session with what you found, plus
   any assumptions a sibling might depend on. Do not /close-work without
   my OK.

   Routing rule: send ALL questions / clarifications to me via
   /send-to-session — do NOT use AskUserQuestion or ask the boss in this
   session (the boss is in the manager session, not yours). Exception:
   system permission prompts must still be approved by the boss in your
   picker.

   Manager: cse_<manager>.
   ```

4. **Record the brief's assumptions in the plan-checkpoint artifact** (or
   post-script them if the artifact already shipped) and spawn — do NOT
   block waiting for verbatim approval of the brief text. The boss can
   redirect mid-flight by replying to the artifact or by /send-to-session
   into the worker. Exception: if the brief commits the worker to an
   irreversible action not in the plan artifact (touching prod, force-
   pushing main, large-scale deletes, cross-repo coordination not in the
   plan), surface that as a separate ask before spawning.
5. **Spawn + dispatch in one call**:
   ```bash
   python3 -m remote_control new-session \
     --dir <repo-root> \
     --subname mgr-<short-tag> \
     --prompt-file <brief-file>
   ```
   The CLI prints `session : cse_<worker>`. Capture it.
6. **Register in the state log**. The `--ticket <N>` argument is mandatory
   per #88: dispatching a worker without a tracking ticket is a bug, and
   `register` will refuse it:
   ```bash
   WSH=~/.claude/skills/manage/scripts/workers.sh
   "$WSH" register cse_<worker> <repo-root> \
     --ticket <N> \
     --brief "<one-line summary>"
   ```
7. **Retitle the worker AND this manager session** so the bracket chain
   reflects the new count. Routine action, no confirmation needed:
   ```bash
   "$WSH" retitle-worker cse_<worker> "<one-line brief>"
   "$WSH" retitle "<this manager's task>"
   ```
   `retitle-worker` shells to `titles set --id <worker> --cwd <worker-dir>
   --sub …` (worker nick tracks its repo); `retitle` shells to `titles set
   --id <mgr> --sub …` with **no** `--cwd` (manager nick from session
   identity, so it survives the watcher). See "Session title convention"
   above for the rendered shape, the optional `--epic`, and the cross-repo
   case.
8. **Arm the monitoring loop, if not already armed.** The loop is what
   wakes the manager every ~20 minutes to check for silently-dead workers
   (see **Play: tick**). Idempotent — safe to "arm" on every dispatch; the
   marker file just records the latest arm time.
   ```bash
   state="$(~/.claude/skills/manage/scripts/workers.sh loop-state | head -1)"
   if [[ "$state" == "disarmed" ]]; then
     # Enter /loop in dynamic mode (self-paced via ScheduleWakeup at end of
     # each tick). The boss doesn't need to invoke /loop — the manager
     # does it on first dispatch.
     # → invoke the /loop skill with body "/manage tick" and no interval.
     ~/.claude/skills/manage/scripts/workers.sh loop-arm
   fi
   ```
9. **Tell the boss** the worker is dispatched, its `cse_*`, and that it will
   report back via send-to-session when its work is done.

### Play: **status** — what are my workers doing

When the boss says "status" / "what are my workers up to":

1. `workers.sh list` — active workers from the state log.
2. **Join with live session state**: `python3 -m remote_control sessions
   --json` and match on `cse_*` to pick up each worker's `worker_status`
   (running / idle / requires_action / disconnected) and `last_event_at`.
3. **Note absences**: a worker present in the state log but absent from the
   live session list has already self-exited (its one-shot server ended
   after the single session). Surface as "ended without reporting" and ask
   the boss whether to **check** (no longer possible — session is gone) or
   **forget** (drop from tracking).
4. Present a compact table to the boss: worker id (last 8 chars), ticket #,
   repo, brief, live status, last activity.

### Play: **check** — handshake with one worker

When the boss says "check on worker N" or "ask cse_X what's going on":

1. **Resolve the target.** Partial id (last 4–8 chars) or ticket # → look
   up via `workers.sh get` and `workers.sh list`.
2. **Check the worker isn't mid-burst.** `sessions --json` for that id. If
   `worker_status=running`, surface that — nudging now interleaves with
   in-progress work. Default: wait for idle unless the boss says go.
3. **Compose the check-in message** (concrete beats vague):
   ```
   Manager check-in: please report
   (1) where you are vs the task,
   (2) what you've actually verified (tests / browser),
   (3) any blockers.
   If you believe you're done, list what changed (files + commits) and
   what you tested — DO NOT /close-work yet; wait for my OK.
   ```
4. **Confirm with the boss**, then dry-run, then submit (per the
   [[send-to-session]] "confirm before sending" rule):
   ```bash
   python3 -m remote_control sessions submit cse_<worker> --stdin --dry-run < msg.txt
   python3 -m remote_control sessions submit cse_<worker> --stdin             < msg.txt
   ```
5. **Record the check**: `workers.sh update cse_<worker> --status
   awaiting-reply --note "asked for status at <ts>"`.
6. The reply will land here as a user-turn with the `[from cse_<worker>]`
   header — route it via **Receiving a worker report** below.

### Receiving a worker report

When a user-turn arrives with header `[from cse_<worker> — reply via
send-to-session]`:

1. **Do NOT treat it as a new boss request.** It is a status report from a
   worker you dispatched.
2. **Record it.** `workers.sh update cse_<worker> --status <reported> --note
   "<one-line distillation>"`.
3. **Surface it to the boss verbatim.** Quote the worker's reply; include
   the worker id, ticket #, and the relevant state-log row. If the report
   includes a working result (UI change, demo, file diff, schema migration,
   etc.), capture it as an **MVP POC** (see "POC checkpoints") — inline
   the diff/summary in chat when small, or save under
   `~/.ai-harness/manager/<MGR-CSE_ID>/pocs/` and link.
4. **Recommend the next action** (don't enumerate every option as a
   question): pick the one the report warrants — typically **close**,
   another **check**, re-dispatch with new instructions, or **forget** —
   and offer the boss a one-word redirect ("close it" / "check again" /
   "redirect to X"). The MVP artifact is the boss's review surface; your
   one-line recommendation is the default action.

### Play: **close** — confirm done + instruct teardown

When the boss says "close worker N" (after a satisfactory report):

1. **Confirm once more**: "Close worker cse_<…> (ticket #<N>, brief: …) —
   instruct it to /close-work?"
2. **On OK, send the close instruction** via send-to-session:
   ```
   Boss confirmed: please run /close-work now. Follow the close-work flow
   end-to-end (review → test gate → TODO sweep → merge → push → release
   leases → close ticket #<N> → release claim).
   ```
   You don't need to ask the worker to "reply when done" explicitly —
   [[close-work]]'s Phase 5 step 9 already requires it to submit a
   `/close-work complete:` user-turn back into this session via
   send-to-session once the merge + cleanup land. Same for handoffs
   (Handoff brief section).
3. Confirm + dry-run + submit (same as **check**).
4. **When the worker's `/close-work complete:` (or `handoff:`) report
   arrives** here as a `[from cse_<worker>]` user-turn:
   - **close** path: mark closed in the state log:
     ```bash
     ~/.claude/skills/manage/scripts/workers.sh close cse_<worker> \
       --reason "merged <sha>"
     ```
   - **handoff** path: mark with `update --status handoff --note "<reason>"`
     and leave the row active (someone else may pick it up via
     [[resume-work]]); only **close** in the state log once that successor
     worker delivers (or the boss decides the ticket is dead).
5. **Archive the worker's cloud session** (close path only — NOT handoff).
   The one-shot server self-exits when its single session ends, but the
   cloud session record stays *active+idle* in the picker until archived.
   Auto-archive on close so the picker stays clean:
   ```bash
   cd ~/dev/ai-harness && python3 -m remote_control sessions archive \
     cse_<worker> --dry-run     # always dry-run first on an unfamiliar id
   cd ~/dev/ai-harness && python3 -m remote_control sessions archive \
     cse_<worker>
   ```
   Skip the archive for the **handoff** path — the successor worker (or
   [[resume-work]]) may need the session record live to pick up context.
6. Tell the boss the merge SHA + the state-log row's new state +
   "archived" so they know the picker is clean. The worker's one-shot
   server self-exits separately.
7. **Auto-disarm the monitoring loop if this was the last worker.** Check
   `workers.sh list --json | jq 'length'`. If `0`, run
   `workers.sh loop-disarm` and do NOT call ScheduleWakeup at the end of
   this turn — the next tick won't fire. If `>0`, leave the loop armed.
8. **Retitle this session** to reflect the new (lower) active-worker count:
   ```bash
   ~/.claude/skills/manage/scripts/workers.sh retitle "<this manager's task>"
   ```
   See "Session title convention" above.

### Play: **forget** — drop a worker from tracking without closing

When the boss says "I took over worker N manually, drop it" or "that one's
dead, forget it":

1. `workers.sh forget cse_<worker> --reason "<short reason>"`.
2. **Auto-disarm if last worker** (same check as **close** step 6).
3. **Retitle this session** to reflect the new active-worker count:
   ```bash
   ~/.claude/skills/manage/scripts/workers.sh retitle "<this manager's task>"
   ```
4. Confirm: row is gone from the active list; the live session (if any) is
   untouched — this is purely a manager-side bookkeeping action.

### Play: **tick** — periodic health check on workers

Triggered when the boss (or the manager itself on first dispatch) invoked
`/loop /manage tick` (dynamic mode). Fires every ~20 minutes while there are
active workers. **Stays silent unless an anomaly is found** — periodic
"everything fine" reports would drown the boss's chat.

1. **Get active workers from the state log**:
   ```bash
   ~/.claude/skills/manage/scripts/workers.sh list --json
   ```
   If the array is empty, run `workers.sh loop-disarm` and **end the loop**
   by *not* calling ScheduleWakeup at the end of this turn. No workers,
   nothing to monitor — wake-ups are pure waste.

2. **Get live session state**:
   ```bash
   cd ~/dev/ai-harness && python3 -m remote_control sessions --json
   ```

3. **Join + classify each active worker**. Anomaly categories (mutually
   exclusive; pick the most specific):

   - **`exited-without-reporting`** — worker is in the state log as
     `active` but absent from `sessions --json`. The one-shot server
     self-exited. Either ran /close-work but skipped its Phase 5 step 9
     report, or died before merging. **Suggested action**: check the
     worker's transcript for the merge SHA (see [[session-triage]]); if
     merged → `workers.sh close cse_<w> --reason "merged, no report"`,
     else → `workers.sh forget cse_<w>` after the boss confirms.
   - **`disconnected-stale`** — `connection_status=disconnected` AND
     `last_event_at` older than 30 min. Bridge dropped. May be alive
     on another host or dead. **Suggested action**: ask the boss whether
     to forget or try a check (may not deliver).
   - **`hung`** — `worker_status=running` AND `last_event_at` older than
     60 min. Not progressing. **Suggested action**: check (send a "are
     you still working?" nudge — confirm with boss first since the worker
     is technically busy).
   - **`awaiting-permission`** — `worker_status=requires_action`. The
     worker is paused on a permission prompt nobody is driving.
     **Suggested action**: the boss must open the worker's session in the
     picker and approve. The manager can't drive permission prompts from
     here.
   - **`silent-done`** — `worker_status=idle` AND `connection_status=
     connected` AND `last_event_at` older than 60 min AND the state-log
     status is not already `reported-done` or `closed`. May have finished
     but never sent the report. **Suggested action**: **check** play —
     ask it to report status.

4. **If zero anomalies**:
   - Update the state-log marker: append a `tick` update on one of the
     workers, OR (simpler) just write the `armed_at` again via
     `workers.sh loop-arm` so the marker file's mtime reflects "last
     tick fired healthy at <ts>".
   - **Produce no boss-facing output.** A silent re-arm is the point.
   - Call `ScheduleWakeup(delaySeconds=1200, prompt="/manage tick",
     reason="manager loop — N workers active, all healthy")` to schedule
     the next tick.

5. **If one or more anomalies**:
   - Surface them to the boss as a compact table: worker id (last 8
     chars), ticket #, repo, anomaly category, suggested action.
   - Wait for the boss's decision before doing anything. **The tick play
     never auto-checks/closes/forgets** — diagnosis only.
   - Still call ScheduleWakeup at the end so the next tick fires (unless
     the boss says "stop monitoring", in which case `workers.sh
     loop-disarm` and don't re-arm).

### Boss commands that affect the loop

- "stop monitoring" / "disarm the loop" → `workers.sh loop-disarm`, don't
  call ScheduleWakeup at end of this turn.
- "what's the loop doing" → `workers.sh loop-state` + a one-line summary
  (armed / disarmed / next tick at).
- "tick now" → run the **tick** play once outside its schedule (don't
  re-arm if the boss explicitly asked for a one-shot).

## Confirmation rules (manager-specific)

- **Auto-spawn IS allowed — after the plan-checkpoint artifact lands.**
  Applies to both oneshot `claude -p` and full-session dispatches. The
  artifact (with assumptions + dispatch-mode per unit listed) is the
  boss's redirect surface; dispatch happens on the same turn. What is
  NOT allowed: spawning before any plan artifact exists, or spawning
  units whose scope was never reflected in any artifact the boss saw.
- **Surface every spawn in the next chat turn.** Full session:
  "Dispatched cse_<X> for unit Y; brief at <POC path>." Oneshot:
  "Oneshot for unit Y → <research/mvp POC path>, summary: …". The
  boss redirects by reply, not by pre-approval.
- **Default to full session when in doubt about mode.** Oneshot is for
  small read-only-ish work whose output you'd read once. If you'd want
  test gates, browser verification, multi-file edits, or /close-work's
  merge flow — full session.
- **Never auto-commit from a oneshot.** Even when `--permission-mode
  acceptEdits` is used and the oneshot has edited files in place,
  surface `git diff --stat` and the touched paths; commit only after
  explicit boss OK.
- **Abort, don't chain.** If a oneshot's scope grows mid-flight, abort
  and re-dispatch as a full session — don't bolt three more `claude -p`
  calls onto it.
- **Escalate irreversible scope before dispatching.** Touching prod,
  force-pushing main, large-scale deletes, cross-repo coordination not
  reflected in the plan artifact — those need an explicit ask before the
  worker is told to proceed.
- **Never auto-close.** A worker reporting "I think I'm done" is not
  blanket approval to instruct /close-work — the boss confirms. Surface
  the MVP-checkpoint artifact + a one-line recommendation; instruct
  close-work only on explicit OK.
- **Never auto-merge.** That's [[close-work]]'s rule; the manager
  inherits it transitively (the worker's /close-work still asks the
  boss).
- **Surface worker replies verbatim.** Distill for the state log, but do
  not filter what the boss sees. The boss is the final judge of "done".
- **One worker = one ticket — mandatory** (per #88). Every full-session
  worker dispatch MUST carry a `--ticket <N>` value pointing at a real
  GitHub issue on the right Project board. `workers.sh register` will
  refuse to record the worker without one, and `retitle-worker` requires
  the ticket to compose the `[#<n>]` bracket. The earlier "trivial
  one-shots can skip" loophole is gone — if a unit isn't worth filing a
  ticket for, it isn't worth a full-session worker (downgrade it to a
  `claude -p` oneshot instead). If a worker's scope grows mid-task, file
  a follow-up issue rather than letting one ticket cover two workers'
  worth of work.

## When NOT to use this skill

- **A single small task you can do yourself in this session.** Spawning a
  worker is heavyweight; don't dispatch what you can finish in one turn.
- **Resuming an existing interrupted thread.** That's [[resume-work]] — it
  reconstructs context from the prior transcript. The manager only spawns
  *fresh* workers.
- **Sending a one-off message to an unrelated existing session.** That's
  [[send-to-session]] directly. The manager only tracks workers it spawned
  itself.
- **A paused-on-usage-limit session.** The usage-limit monitor auto-resumes
  those; manual nudges fight its backoff (see [[send-to-session]]'s "When
  NOT to use").

## Don't

- **Don't dispatch the same task to two workers** — they'll race on the
  ticket and the merge. One worker per unit.
- **Don't poll workers on a timer.** Workers are *expected* to report back
  via send-to-session; if they haven't, surface the silence as a signal —
  don't paper over it with a nudge.
- **Don't reuse a worker's session for a second task** after it reported
  done. Spawn a fresh worker (and fresh one-shot server) for each task —
  that's the lifecycle [[new-session]] is designed for.
- **Don't quote a worker's reply inside another tool-call body** without
  attributing it. The boss should see worker reports as worker reports, not
  as something the manager said.
- **Don't write to the worker's repo or worktree from the manager session.**
  The worker owns its cwd; manager edits there break the worker's
  view of "no concurrent writes".
