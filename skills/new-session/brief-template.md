# Worker brief template

Canonical shape for a manager's first-turn brief to a spawned worker (see
[[new-session]] "Manager / worker pattern" and [[manage]]). Fill in every
section — an empty "Sibling workers" section is a signal the manager forgot
to check its roster, not proof there are no siblings.

```
## Goal

<the manager's overall goal — one paragraph. The worker's task is a SLICE of
this; say so explicitly so the worker can tell when its own task is drifting
into someone else's slice.>

## Context

<repo, branch/worktree, any prior art the worker should read before starting
(files, tickets, prior commits). Link, don't paste, anything long.>

## Sibling workers

<one line per OTHER live worker the manager has spawned for this same goal.
Empty ("None — you are the only worker on this goal right now.") is a valid
value, but state it explicitly rather than omitting the section.>

- `<subname>` (cse_<id>) — <one-line responsibility>. Repo: <dir>.
- `<subname>` (cse_<id>) — <one-line responsibility>. Repo: <dir>.

## Settled decisions

<decisions already made by the manager or a sibling that this worker must
NOT re-litigate — e.g. a shared ratio/formula, a schema choice, a naming
convention. If a sibling already decided something in your task's area,
name it here so you don't recompute it differently.>

## Task

<the concrete, specific work for THIS worker.>

## Constraints

<stay in lane: if this task starts to overlap a sibling's scope listed
above, STOP and report the overlap to the manager via /send-to-session
instead of proceeding — don't silently resolve it yourself.>

## Report-back format

Before /close-work, send a status report via /send-to-session with:
- what changed (files / commits)
- what you tested
- **state of my work**: done / in-progress / decisions made — so the
  manager's roster stays accurate even if this session later disconnects
  (makes any future /takeover or /relaunch brief far more useful)
- **assumptions siblings might depend on** — anything you decided that a
  sibling worker's task might need to know, so the manager can relay it
- any blockers

Manager: cse_<manager-id> (the [from …] header above shows where to reply).
```

## Notes for the manager filling this in

- **Worker roster.** Track live workers in this session's todo list (one
  todo per worker, kept until a terminal event — reported/closed/forgotten).
  That's the roster you transcribe into each new worker's "Sibling workers"
  section — don't reconstruct it from memory each time.
- **New worker, existing siblings.** Before spawning, re-read the roster and
  list every other *live* worker under "Sibling workers" — subname,
  `cse_*`, one-line responsibility, repo. Include any "Settled decisions"
  a sibling already made that overlaps this worker's area.
- **Mid-flight forwarding to an existing worker.** When you `send-to-session`
  new information into a worker that's already running (a sibling's
  decision, a changed constraint), name which sibling produced it and
  whether it supersedes something the worker already assumed:
  ```
  Update from your manager: cse_<sibling> (subname: <x>) just settled on
  <decision>. This supersedes <what you may have assumed>. Adjust your
  work accordingly.
  ```
- **Relaying a decision that affects siblings.** When a worker reports back
  with a decision other live workers depend on, proactively `send-to-session`
  it to each affected sibling — don't assume they'll discover it on their
  own. Update the roster (todo list) so the next brief you write reflects it
  under "Settled decisions".
