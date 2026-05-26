# Session-manager: case catalog & guidelines

Working notes for the autonomous **session manager** (`python3 -m remote_control
manager`). It reads every Claude Code session's state and decides what to do.
This file catalogs the **cases** it runs into and is where we write the
**guidelines** for how it should handle each. Fill in the `GUIDELINE` blocks as
you review; the code's defaults are noted under "Current behavior".

Status (2026-05-25): the manager classifies, runs an investigator to decide, and
logs the decision. Each recommendation is split into a **MANAGER** half (run by a
`claude -p` executor — auto-run in the loop, gated by `MANAGER_EXECUTE_ENABLED` +
not dry-run) and a **SESSION** half (delivered to the session, human-authorized in
the ui). Answering a structured question live is still gated off (see *Open
questions → submission shape*).

---

## How it classifies (signal → case)

All signals come from `GET /v1/code/sessions` plus a per-session
`GET /v1/code/sessions/{id}` (which carries `requires_action_details`). Scope is
the repo allowlist (`active-dirs.txt`, host-aware). Priority order top to bottom:

| Signal | Case | Default action |
|---|---|---|
| `post_turn_summary` = usage/session limit | **D. Limit-paused** | DEFER (usage-monitor owns it) |
| `worker_status == requires_action` (past answer-grace) | **A. Waiting on a question** | ANSWER (investigate → pick option) |
| `connection_status == disconnected` (past rescue-grace) | **C. Broken / stale** | RESCUE (fork → resume → archive) |
| `worker_status == idle` (past done-grace) | **B. Idle, no question** | REVIEW (done? → /close-work, else next) |
| `worker_status == running`, or too-recent | **E. Working / settling** | SKIP |

Guards on every action: **grace** (don't act until the state has persisted),
**cooldown** (don't re-act on the same session within N), **max-actions-per-tick**.

---

## Recommendation target — MANAGER vs SESSION

Every analysis splits its recommendation by **who acts**, in two titled,
sequential sections (`MANAGER:` then `SESSION:`); either may be `none`:

- **`SESSION:`** — guidance delivered **into the live session** for the agent to
  do next *while it is still alive*: answer its pending question, **run
  `/close-work`** to wrap up + deliver a finished thread, the next instruction,
  "write down notes / a hand-off", "file a follow-up ticket and link it to the
  current one", "separate (and don't touch) the prod work". Delivered as a user
  turn — **human-authorized** in the manager-ui ("→ send to session").
- **`MANAGER:`** — lifecycle ops the session **cannot do to itself**, carried out
  by the manager: **archive** the session once its work is finished (e.g. after
  `/close-work` is confirmed done), or **fork + resume** a broken one. **Auto-run
  in the loop**, double-gated (`MANAGER_EXECUTE_ENABLED=1` **and** not dry-run);
  otherwise shadow-logged.

> **`/close-work` is a SESSION action, not a MANAGER one.** The session has the
> skill and the context to wrap *itself* up (commit/merge/push, switch the bridge
> worktree back, release the agent claim, close the tracking ticket), so the
> manager *asks the session* to run it. **Archiving** the now-finished session is
> the MANAGER follow-up — only once close-work is confirmed done **and we're
> ready**; otherwise `MANAGER:` is `none` and the manager just re-checks next pass.

So a done-looking thread is typically a **two-pass** flow:
`SESSION:` run /close-work (+ any notes / follow-up ticket) → and once that has
landed, on a later pass `MANAGER:` archive the session.

**GUIDELINE:**
- Put in `MANAGER:` only lifecycle ops the session can't do itself **and** that
  are safe to run unattended: archiving a session whose work is *confirmed*
  delivered, or fork + resume of a broken one. If a step touches `main`, pushes,
  secrets, prod, or is destructive (🟠 orange / 🔴 red below), it does **not** go
  in `MANAGER:` — leave it as a `SESSION:` recommendation or a human escalation.
  Keep clearly-prod work in `SESSION:` and call it out explicitly (prod is
  note-only — the MacBook; never auto-execute it).
- **Never** put `/close-work` (or any in-session wrap-up) in `MANAGER:` — that's a
  `SESSION:` action. **Don't archive** until the work is truly finished; if you're
  unsure it's done, set `MANAGER:` to `none`.
- A waiting question (Case A) is answered via the `SESSION:` half (the chosen
  option) — the manager doesn't answer questions through the executor.

---

## Case A — Waiting on a question  *(most common)*

**Signal:** `worker_status == requires_action`. The authoritative detail is the
API field `requires_action_details`: in practice a **tool call**, almost always
`AskUserQuestion`, carrying *structured multiple-choice* questions
(`{tool_name, tool_use_id, input.questions:[{question, header, options:[{label,
description}], multiSelect}]}`). It is **not** free text.

**Current behavior:** read the structured question from the API → run a headless
`claude -p` investigator in the repo's **main checkout** (`--permission-mode
plan`, read-only, so it never touches the paused session's worktree) → it picks
one option per question → the choice is validated against the real labels →
*reported only* (submission gated off; shape unverified).

**The key axis is stakes.** The same case spans harmless to irreversible:

This stakes axis is the **shared policy**: it is the same scale the in-session
permission gate ([`perm-gate.md`](perm-gate.md)) enforces as risk tiers
(🟢 green / 🟡 yellow / 🟠 orange / 🔴 red). One policy, two enforcement points.

| Stakes | Risk tier | Example option | Auto-pick? |
|---|---|---|---|
| Reversible / non-destructive | 🟢 green | "Post a progress comment", "Just commit on the branch", "Plan the cutover (don't execute)" | **Yes** — auto-OK |
| Medium (titles, labels, local merges) | 🟡 yellow | "Apply the 23 renames", "Commit + merge locally, don't push" | **Yes, if local & reversible** (no push); escalate if it touches `main` |
| High / outward-facing | 🟠 orange | "merge --no-ff to main + push", "release claim + close ticket" | **No** — require a human |
| Destructive / irreversible | 🔴 red | "Execute the live purge now (history rewrite + force-push all branches)" | **Never** — require a human |
| Prod / deploy | 🔴 red | "mint tokens, SOPS, deploy, flip flag" | **Never** — require a human; per CLAUDE.md prod is note-only (the MacBook) |

**Real examples seen 2026-05-25** (all AppOne, all `AskUserQuestion`):

1. **#66 history-purge** (`cse_01CzRGg2`) — *"audit + recipe done, how to proceed?"*
   - `Land audit, defer force-push (Recommended)` — commit 3 docs + comment on #66; no rewrite. *(non-destructive)*
   - `Also run full-recipe dry-run` — one more non-destructive dry-run first. *(non-destructive)*
   - `Execute the live purge now` — **rewrite history + force-push all branches.** *(DESTRUCTIVE)*
2. **#88 delivery** (`cse_01Ptav`) — *"proceed with Phase 5 delivery?"*
   - `Yes, deliver it all` — commit, **merge --no-ff to main, push**, close #88. *(high)*
   - `Commit + merge, I'll push` — stop before push. *(medium)*
   - `Just commit for now` — branch only. *(low)*
3. **#82 app-two API auth** (`cse_01XwDew`) — *"how to proceed with implemented work?"*
   - `Commit all 3 repos` *(medium)* / `Post progress to #82` *(low)* /
     `Plan Phase 4 cutover` *(low, no execute)* / `Run app-two tests too` *(low)*.
   - Note remaining work is a **prod deploy** (note-only — the MacBook).
4. **Label renames** (`cse_01Xmiu`) — two questions:
   - Q1 label-less repos: `Skip them (AppOne only)` / `Label them first` / `Infer prefix from title only`.
   - Q2 apply: `Apply as shown` *(edits 23 GH titles)* / `Let me adjust first` *(safe — waits)*.

> Investigator quality looked good in trials (it chose the non-destructive
> recommended options and reasoned about scope), so the *deciding* half is sound;
> the gap is *how to submit* the choice (below) and the *policy* (guidelines above).

**GUIDELINE:**
- **Auto-pick allowed only when** the chosen option is 🟢 green / 🟡 yellow —
  routine, reversible, non-outward-facing (a local commit, a progress comment, a
  plan-but-don't-execute, a local-only merge that doesn't touch `main`). Prefer
  the `(Recommended)` option when present and it meets this bar.
- **Always require a human for** 🟠 orange and 🔴 red: any push, merge to `main`,
  releasing a claim / closing a ticket, secrets (SOPS/age), prod / deploy, and
  anything destructive or irreversible (history rewrite, force-push, bulk delete).
- **Multi-question prompts:** answer only if confident on *all* questions and all
  are green/yellow; if any is orange+ or uncertain, escalate the whole prompt.
- **Answer-grace** (how long to let a human go first): operator tunable via
  `MANAGER_ANSWER_GRACE_SECS` (default 10m).

---

## Case B — Idle, no pending question  *(maybe done)*

**Signal:** `worker_status == idle`, connected, not limit-paused, idle past the
done-grace. **Current behavior:** investigator reviews recent work and returns
`DONE: <summary>` (→ manager would recommend running `/close-work`) or `NEXT:
<instruction>` (→ would post the next step). Reported only for now.

**GUIDELINE:**
- "Idle" is **done** when the work landed (committed/merged as the thread
  intended) with nothing mid-edit; it's **quietly stuck** when there's uncommitted
  work, an unanswered question in the last turn, or an error it didn't recover
  from (→ a `SESSION:` nudge, or RESCUE if unresponsive).
- Split the call: `SESSION:` for everything the session does to wrap *itself* up
  — write notes, file a follow-up ticket, verify backups, separate prod work, and
  **run `/close-work`**; then `MANAGER:` **archive** the session, but only on a
  later pass once close-work is confirmed landed (else `MANAGER:` = `none`). A
  pure NEXT nudge is a `SESSION:` rec.
- done-grace: `MANAGER_DONE_GRACE_SECS` (default 30m).

---

## Case C — Broken / stale

**Signal:** `connection_status == disconnected` past the rescue-grace (later:
also "last turn was an API error"). A rescue is a **`MANAGER:`** action (the
executor does it): `fork` the session → resume work on the fork → archive the
stale original.

**GUIDELINE (your call):**
- rescue-grace before declaring it dead? `MANAGER_RESCUE_GRACE_SECS` (default 1h).
- Fork into the main checkout or a fresh worktree? ____
- Archive the original automatically, or only flag it? ____
- What resume prompt should the fork get? ____

---

## Case D — Usage-limit paused

**Signal:** `post_turn_summary` says usage/session limit. **Behavior:** DEFER —
the existing usage-limit monitor already detects + resumes these; the manager
must not fight it. *(No guideline needed unless we merge the two systems.)*

---

## Case E — Working / settling

**Signal:** `worker_status == running`, or any state that's too recent (within
its grace). **Behavior:** SKIP. A quiet `running` worker is indistinguishable
from a long tool call, so we never touch it (avoids false positives). *(No
guideline needed.)*

---

## Open questions / findings

- **Submission shape (BLOCKER for live answering).** How do you submit an
  answer to an `AskUserQuestion`? Two attempts, both `HTTP 200` but the session
  **stayed `requires_action`** (did not resolve):
  1. free-text user event (`/events`, plain text) — *no*.
  2. a `user` event carrying a `tool_result` content block keyed to `tool_use_id`
     with the chosen label as text — *no*.
  Next: reverse-engineer the real shape from a session where an `AskUserQuestion`
  was answered normally (read its recorded resolution event), or capture what the
  official client POSTs. Until then `MANAGER_SUBMIT_ENABLED=0`.
- **Permission prompts (not yet seen).** `requires_action` could also be a *tool
  permission* approval (not `AskUserQuestion`). Needs its own detection + an
  approve/deny submission. Guideline + shape both TBD.
- **Cloud sessions.** `requires_action_details` comes from the API, so it works
  for cloud sessions too; the old local-transcript read is retired.

---

## How we test responses to all scenarios

Three layers (see also `tests/test_manager.py`):

1. **Classification / planning (deterministic, offline).** Synthetic session
   dicts → `classify` / `plan_for_session` assert the right case + action +
   guard behavior (grace, cooldown, allowlist). Add one fixture per row of the
   tables above. No network, no LLM.
2. **Investigator choices (LLM, non-deterministic) — scenario replay.** A corpus
   of saved `requires_action_details` (the real ones above + hand-written edge
   cases) under `scenarios/`. `manager --replay <scenario.json> --repo R` runs
   the investigator against a saved question **offline** (no live session) and
   prints the option it picks — so we can eyeball/curate its choices per scenario
   and lock in expectations, without ever touching a real session.
3. **End-to-end submission.** Blocked on the submission-shape question above;
   once known, one guarded live probe per case (safe option only).

To add a scenario: drop a `requires_action_details` JSON into `scenarios/` (the
monitor can dump real ones it sees), then `manager --replay` it.
