---
name: new-session
description: >-
  Spawn a one-shot picker-visible Claude Code session from inside the current
  session (or against another dir). Wraps `python3 -m remote_control
  new-session`, which launches a detached `claude remote-control --capacity 1`
  server with a non-`mm-` name — picker-visible like the launchd-managed
  servers, supervisor-invisible (no active-dirs edit), self-exits when its one
  session ends. Use when the user wants to "create a new session", "spin a
  worker session", "/new-session", "/remote-session", "spawn a session in
  <dir>", or asks for a fresh picker row without committing the dir to the
  allowlist.
---

# new-session (spawn a one-shot picker-visible session)

A `claude remote-control` server with `--capacity 1` and a name that starts
with `oneoff-` (not `mm-`). That combination is the whole trick:

- **Picker-visible**: it registers with the cloud the same way the launchd-
  managed servers do, so the Claude desktop/mobile app shows a row for it
  alongside `mm-ai-harness`, `mm-FamilyFund`, etc.
- **Supervisor-invisible**: the supervisor's process scan (`procutil.py`
  `_RUNNING_RE`) only matches `--name mm-*`, so it never adopts, reaps, or
  recycles this server — even on a tick where the active-dirs file changes.
- **Self-cleaning**: capacity 1 means the server accepts exactly one session;
  when that session ends, the server exits and the picker row disappears.

This is the right primitive when an agent wants another session (for itself
or a parallel worker) without touching `active-dirs.txt`.

## Tool

```
python3 -m remote_control new-session [--dir PATH] [--name SLUG]
                                      [--spawn worktree|same-dir]
                                      [--permission-mode MODE]
                                      [--dry-run]
```

- `--dir` — where to anchor the server. Default: the current working directory.
- `--name` — server name. Default: `oneoff-<host>-<8hex>`. Refuses any name
  starting with `mm-` (the supervisor would otherwise adopt it).
- `--spawn` — `worktree` (git-aware isolation; creates a `.claude/worktrees/`
  subdir per session) or `same-dir`. Default: auto from a git probe of `--dir`
  (same rule the supervisor's discovery uses).
- `--permission-mode` — pass-through. Default `acceptEdits` (the supervisor's
  posture for unattended servers).
- `--dry-run` — print the exact command + cwd + log path; spawn nothing.
  Always run dry-run first on an unfamiliar dir.

The launched server logs to `$LOGDIR/<name>.log` (same dir as the supervisor's
own `mm-*` logs). On success the CLI prints the pid, name, cwd, spawn mode,
log path, and the full argv.

## Workflow

1. **Confirm the dir.** If the user didn't name one, default to the current
   worktree's cwd and surface it explicitly ("spawning a session in
   `<cwd>` — OK?"). For a dir under `~/dev`, this is the dir the picker row
   will be anchored to. **Do not silently pick a different dir.**
2. **Dry-run first.** Run with `--dry-run` and show the resulting command +
   log path. Confirm `--spawn` mode matches what the user expects (worktree
   for a normal repo, same-dir for non-git roots).
3. **Launch.** Drop `--dry-run`. The CLI prints the pid + log path. The
   picker row should appear within a few seconds (the server registers with
   the cloud on startup).
4. **Tell the user how to find it.** Name shows up in the app picker as part
   of the host row (e.g. `macmini.local <some-label>`) — they tap it like any
   other env. If the session is for *another* agent (e.g. via `start-work`),
   point them at the agent's instructions in the relevant ticket.

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

Pin a dir + name (useful if you want to find the log later):

```bash
python3 -m remote_control new-session \
  --dir ~/dev/job-search \
  --name oneoff-mini-jobsearch-review
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
  `python3 -m remote_control fork <cse_id>` (see [[resume-work-skill]]).

## Don't

- Don't pass `--name mm-...` — the CLI refuses it; the supervisor would
  otherwise adopt or reap your one-off based on active-dirs membership.
- Don't run repeatedly in the same dir to "stack workers" — each call spawns
  a separate server (separate picker row, separate worktree subdir). If you
  want N concurrent sessions on the same anchor, the right tool is an
  active-dirs entry (capacity 32 by default).
- Don't expect cleanup if you `kill -9` the spawned `claude` server — the
  cloud may leave a ghost session. Let the single session end naturally so
  the server can deregister cleanly.
