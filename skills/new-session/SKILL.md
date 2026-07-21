---
name: new-session
description: >-
  Spawn a one-shot picker-visible Claude Code session from inside the current
  session (or against another dir). Wraps `python3 -m remote_control
  new-session`, which launches a detached `claude remote-control --capacity 1`
  server with a non-`mm-` name — picker-visible like the launchd-managed
  servers, supervisor-invisible (no active-dirs edit). NOTE: it does NOT self-exit
  when its session ends (`--capacity` caps concurrency, not lifetime); the
  supervisor's one-shot reaper sweeps the leftover process. Supports the manager-pattern one-shot dispatch via `--wait` and
  `--prompt` (spawn → wait for the inner cse_id → submit the first turn, in a
  single CLI call). Use when the user wants to "create a new session", "spin a
  worker session", "dispatch a worker with this brief", "/new-session",
  "/remote-session", "spawn a session in <dir>", or asks for a fresh picker row
  without committing the dir to the allowlist.
---

# new-session (spawn a one-shot picker-visible session)

A `claude remote-control` server with `--capacity 1` and a name that starts
with `oneoff-` (not `mm-`, and not `<host>-`). That combination is the whole
trick:

- **Picker-visible**: it registers with the cloud the same way the launchd-
  managed servers do, so the Claude desktop/mobile app shows a row for it
  alongside `mm-ai-harness`, `mm-FamilyFund`, etc.
- **Supervisor-invisible**: the supervisor's process scan (`procutil.py`
  `_RUNNING_RE`) only matches `<host>-<basename>` names, so it never adopts,
  reaps, or recycles this server — even on a tick where the active-dirs file
  changes. The CLI refuses any `--name` that would put your one-off in the
  supervisor's blast radius (`mm-...` or `<host>-...`).
- **Self-cleaning**: capacity 1 means the server accepts exactly one session;
  when that session ends, the server exits and the picker row disappears.

This is the right primitive when an agent wants another session (for itself
or a parallel worker) without touching `active-dirs.txt`.

## Tool

```
python3 -m remote_control new-session [--dir PATH] [--name SLUG]
                                      [--spawn worktree|same-dir]
                                      [--permission-mode MODE]
                                      [--wait [--wait-timeout SECS]]
                                      [--prompt TEXT | --prompt-file PATH]
                                      [--reply-to CSE_ID | --no-reply-to]
                                      [--dry-run]
```

- `--dir` — where to anchor the server. Default: the current working directory.
- `--name` — server name. Default: `oneoff-<nick>-<8hex>`, where `<nick>` is
  this machine's short host-nick (the same value the titles watcher uses to
  render the `[NICK.host]` title prefix — see `config.host_nickname`).
  Refuses any name starting with `mm-` or `<host>-` (the supervisor would
  otherwise reap or adopt it). The nick segment keeps picker rows + log
  filenames disambiguated across parallel hosts without re-embedding the
  full hostname.
- `--spawn` — `worktree` (git-aware isolation; creates a `.claude/worktrees/`
  subdir per session) or `same-dir`. Default: auto from a git probe of `--dir`
  (same rule the supervisor's discovery uses).
- `--permission-mode` — pass-through. Default `bypassPermissions` (the
  supervisor's posture for unattended servers; the global perm-gate hook still
  vets every tool call).
- `--wait` — after launch, poll the server's log file for the OSC-8
  `session_<id>?from=cli` hyperlink the `claude` TUI prints when the inner
  cloud session registers. On success, prints `session : cse_...` so a caller
  can pipe / scrape it. **Off by default**; **implied by `--prompt`** (we
  need the id to submit a turn).
- `--wait-timeout SECS` — polling deadline, default 30s. A timeout exits
  non-zero with the log path so the caller can investigate.
- `--prompt TEXT` / `--prompt-file PATH` — after the inner session registers,
  submit this as its first user turn (same wrapped-event body as `sessions
  submit`; same auth). Implies `--wait`. Mutually exclusive.
- `--reply-to CSE_ID` — identify the sender for the spawned worker. Two
  effects: (1) sets `REMOTE_CONTROL_REPLY_TO=CSE_ID` in the spawned server's
  env so the worker can read its manager id even if the prompt header is
  edited; (2) when combined with `--prompt`, prefixes the prompt body with
  the `[from CSE_ID — reply via send-to-session]` header so the existing
  send-to-session contract still applies. **Default: auto-detect from this
  process's `CLAUDE_CODE_SESSION_ACCESS_TOKEN` JWT** (so spawning a worker
  from inside a Claude Code session "just works").
- `--no-reply-to` — skip both env propagation and the prompt header (use
  when the worker has no caller to report back to).
- `--subname SLUG` — tag the inner session's title with an extra
  `[SLUG]` bracket so the spawned subsession is visually distinguishable in
  the picker / `sessions list`. After registration, the CLI PUTs a title of
  the form `[NICK.host][SLUG] auto-spawned`; the titles watcher's
  re-rendering pass preserves the `[SLUG]` segment across its subsequent
  ~10min ticks via `session_titles.extract_sub_token`. **Requires `--wait`
  or `--prompt`** (the title PUT needs the cse_id; we don't grow a 30s poll
  loop just for an aesthetic tag). **Default: auto-derive from the server
  name** (strip `oneoff-` prefix → `oneoff-mini-3f2a1c8b` becomes
  `[mini-3f2a1c8b]`, `oneoff-mini-ff-emails` becomes `[mini-ff-emails]`).
  Skipped silently if the cwd doesn't resolve to a known repo (no `[NICK]`
  to pair it with).
- `--no-subname` — skip the `[SUB]` title tag (the session title gets only
  the watcher's standard `[NICK.host]` prefix on the next pass).
- `--dry-run` — print the exact command + cwd + log path + reply-to /
  subname / wait / prompt summary; spawn nothing. Always run dry-run first
  on an unfamiliar dir.

The launched server logs to `$LOGDIR/<name>.log` (in `~/dev/ai-harness/logs/`,
same dir as the supervisor's own `mm-*` logs — even when the session itself is
anchored in a different repo, because ai-harness owns the supervisor's log
dir). On success the CLI prints the pid, name, cwd, spawn mode, log path,
and the full argv.

## Workflow

### Common case: agent spawns a one-off in its own worktree

1. **Confirm the dir.** If the user didn't name one, default to the current
   worktree's cwd and surface it explicitly ("spawning a session in
   `<cwd>` — OK?"). **Do not silently pick a different dir.**
2. **Dry-run first.** Run with `--dry-run` and show the resulting command +
   log path. Confirm `--spawn` mode matches what the user expects (worktree
   for a normal repo, same-dir for non-git roots).
3. **Launch.** Drop `--dry-run`. The CLI prints the pid + log path. The
   picker row should appear within a few seconds.
4. **Tell the user how to find it.** Name shows up in the app picker as part
   of the host row (e.g. `mini.local <some-label>`) — they tap it like any
   other env. If the session is for *another* agent (e.g. via `start-work`),
   point them at the agent's instructions in the relevant ticket.

### Manager / worker pattern: spawn + first brief in one shot

When an agent (the **manager**) wants to dispatch a worker session with a
specific brief and get it working immediately:

```bash
python3 -m remote_control new-session \
  --dir ~/dev/some-repo \
  --prompt-file /tmp/worker-brief.md
```

That single call does the whole sequence:

1. Spawns the `oneoff-` server in the named dir.
2. Polls the server's log until the inner session registers with the cloud
   (default deadline: 30s).
3. Auto-detects the manager's own `cse_*` id from its
   `CLAUDE_CODE_SESSION_ACCESS_TOKEN` JWT and:
   - Sets `REMOTE_CONTROL_REPLY_TO=cse_<manager>` in the worker's env.
   - Prefixes the prompt body with `[from cse_<manager> — reply via
     send-to-session]` so the worker knows where to report back.
4. POSTs the prompt as the worker's first user turn.
5. Prints `session : cse_<worker>` so the manager has the id to address
   any follow-up messages to.

The worker then has the brief, knows its caller, and the manager has the
worker's id — all from one CLI call. Compare to the four-step manual flow
(spawn, wait for registration, scrape `sessions list` for the id, submit
the message), each step of which has its own failure mode.

### Multi-worker dispatches: keep siblings in each other's context

When a manager spawns more than one worker toward the same overall goal,
each worker's brief must say who else is working on it. Without this,
parallel workers duplicate work, contradict each other's decisions, or
re-litigate something a sibling already settled. Use
[`brief-template.md`](brief-template.md) — it has a mandatory "Sibling
workers" section (subname + `cse_*` + one-line responsibility per live
sibling) and a "Settled decisions" section for anything a sibling already
decided that the new worker must not recompute differently. See
[[manage]]'s worker-roster practice for how the manager tracks the list
that feeds this section.

**Worker-side expectations** (state these in the brief, or point the
worker at the template):
- Read the "Sibling workers" section before starting. If your task starts
  to overlap a listed sibling's scope, stop and report the overlap to the
  manager via `send-to-session` instead of silently resolving it yourself.
- When reporting back, state assumptions a sibling might depend on, and
  include a short "state of my work" (done / in-progress / decisions made)
  — this keeps the manager's roster accurate even if you later disconnect,
  which also makes any future `/takeover` or `/relaunch` brief far more
  useful.

For a short literal prompt, use `--prompt "..."` instead of
`--prompt-file`. For multi-line prompts, prefer the file form — the shell
quoting on a multi-line `--prompt` argument is fragile.

If you want the manager's id pinned explicitly (e.g. you're forwarding from
some other context, not your own session), use `--reply-to cse_X`. If the
worker has no caller and no reply expectation (e.g. a fully-autonomous task
with no follow-up channel), pass `--no-reply-to` to suppress both the env
var and the header.

See [[send-to-session]] for the receiving side of the same protocol — the
worker uses it to deliver its status report back into the manager's
session.

## Examples

Spawn a one-off in the current worktree (cwd is a git repo):

```bash
python3 -m remote_control new-session
# new-session: launched (pid 12345)
#   name   : oneoff-mini-3f2a1c8b
#   cwd    : /Users/user/dev/ai-harness-worktrees/some-feature
#   spawn  : worktree
#   log    : /Users/user/dev/ai-harness/logs/oneoff-mini-3f2a1c8b.log
```

Spawn + wait for the cse_id without submitting a prompt (useful if the
caller is going to address the worker over a different channel):

```bash
python3 -m remote_control new-session --wait --no-reply-to
# ... pid + name + log lines ...
#   session: cse_01ABCxyz
```

Spawn + brief, with auto-detected reply-to (the manager-pattern):

```bash
python3 -m remote_control new-session \
  --dir ~/dev/job-search \
  --prompt-file /tmp/worker-brief.md
# ... pid + name + log lines ...
#   reply-to: cse_01MANAGER
#   session: cse_01WORKER
#   title  : '[JOB.mini][mini-3f2a1c8b] auto-spawned'
# submitted cse_01WORKER (1234 chars)
```

Override the auto-derived subname with something meaningful:

```bash
python3 -m remote_control new-session \
  --dir ~/dev/job-search \
  --subname adzuna-refactor \
  --prompt-file /tmp/worker-brief.md
# ... title  : '[JOB.mini][adzuna-refactor] auto-spawned'
```

Dry-run a non-git dir:

```bash
python3 -m remote_control new-session --dir /tmp/scratch --dry-run
# spawn  : same-dir   (git probe failed → falls back)
```

## When NOT to use this skill

- **You want the env in the picker permanently.** Add the dir to
  `active-dirs.txt` via [[remote-control-dirs]] — the supervisor then owns
  the server's lifecycle (respawn, idle-recycle, clean shutdown).
- **You want a headless agent that does NOT show in the picker.** Use
  `python3 -m remote_control work start --engine claude --go "<prompt>"`
  instead — that's a `claude -p` worker, not a picker-visible server.
- **You want to fork an existing session's history.** Use
  `python3 -m remote_control fork <cse_id>` (see [[resume-work]]).
- **You want to send a message into an existing live session.** Use
  [[send-to-session]] — `new-session --prompt` is for first turns into a
  session you're spawning, not into one that already exists.

## Don't

- Don't pass `--name mm-...` or `--name <your-host>-...` — the CLI refuses
  both; the supervisor would otherwise adopt or reap your one-off based on
  active-dirs membership.
- Don't run repeatedly in the same dir to "stack workers" — each call spawns
  a separate server (separate picker row, separate worktree subdir). If you
  want N concurrent sessions on the same anchor, the right tool is an
  active-dirs entry (capacity 32 by default).
- Don't expect cleanup if you `kill -9` the spawned `claude` server — the
  cloud may leave a ghost session. Let the single session end naturally so
  the server can deregister cleanly.
- Don't set `--wait-timeout` aggressively low. Cold-start registration takes
  several seconds; an unrealistic timeout will fire spurious failures even
  when the worker comes up fine.
