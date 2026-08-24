---
name: revive-bridges
description: >-
  Revive Claude Code bridge sessions that show `conn=disconnected` / "Can't
  reach your computer" after the host was rebooted, slept, lost network, or
  had its `claude remote-control` processes killed. Reattaches the ORIGINAL
  `cse_*` in place (`claude remote-control --session-id <cse> ` from the
  session's own cwd), which preserves the thread, its manager/worker roster,
  and drains any user turns queued while it was down. Also diagnoses the
  look-alike failure where the HOST was signed out ("Your computer needs to
  sign in again") and every bridge dies at the same instant. Use when the user says
  "the picker is all warning triangles", "my sessions can't reach my
  computer", "revive/reattach the dead bridges", "everything went
  disconnected after the reboot", or names one dead `cse_*` to bring back.
  Distinct from [[relaunch]] and [[takeover]], which spawn NEW sessions and
  abandon the old id — use this FIRST; those are fallbacks.
---

# revive-bridges (reattach disconnected bridges in place)

A bridge session lives server-side; the local `claude remote-control` process
is only the transport. When that process dies (reboot, sleep, kill, network
drop) the session shows `conn=disconnected` and the app says "Can't reach your
computer". The session **cannot self-recover** — but nothing is lost. Starting
a new `remote-control` process bound to the same `--session-id` restores the
transport and the session drains whatever turns queued in the meantime.

## Why NOT relaunch/takeover first

- [[relaunch]] and [[takeover]] mint a **new `cse_*`**. Any manager roster
  (`~/.ai-harness/manager/<cse>.jsonl`) and any worker that reports back via
  `--reply-to <old cse>` still point at the dead id, so you inherit a
  re-pointing chore for every manager thread you touch.
- `python3 -m remote_control takeover` additionally **classifies** each stale
  session and marks trivial-looking ones archive-only from a single last turn
  ("community", "resume", "ok"). Run on a picker full of freshly-disconnected
  bridges it will archive **live manager threads**. Never batch-takeover a
  post-reboot picker.

Reattach in place. Relaunch only if reattach genuinely errors on something
other than a stale lock.

## Procedure

### 1. Inventory

```bash
cd ~/dev/ai-harness && python3 -m remote_control sessions
```

Every `conn=disconnected` row is a candidate.

**The `host:` column lies.** It infers host from the *git repo name*, so a
session whose cwd dir name differs from its repo name (e.g. cwd
`~/dev/divorcio`, repo `divorce-prep`) is mislabelled `bridge -> other host`
when it is local. Never skip a row on the strength of that label — resolve the
real cwd instead (step 2). After a successful reattach the row flips to
`bridge -> this host` on its own.

### 2. Resolve each session's cwd

```bash
python3 -m remote_control relaunch --from <cse_id> --dry-run --show-brief | grep '^  cwd'
```

`--dry-run` spawns nothing. A cwd under another user's home (e.g.
`/Users/claudio1/...`) is the genuine other host — leave it for that host's
dispatcher. Confirm the dir still exists before reattaching (worktrees get
removed by /close-work).

### 3. Reattach

```bash
cd <cwd> && nohup claude remote-control --session-id <cse_id> \
    --permission-mode bypassPermissions > /tmp/reattach-<short>.log 2>&1 &
```

One process per session; they can all be launched in one batch. Verify after
~60s — `conn=connected` is success:

```bash
python3 -m remote_control sessions | grep -B4 conn=disconnected
```

On failure the log is the whole story: `cat /tmp/reattach-<short>.log`.

### 4. Stale-lock failures

```
Error: Session <cse> is already being served by another `claude remote-control`
instance (pid NNNN) in <dir>. Use that terminal, or stop it first.
```

Check whether that pid is real: `ps -o pid=,lstart=,command= -p NNNN`.

- **Pid gone** → stale lock. Reattach again; the lock clears.
- **Pid alive** → a supervisor pool server (`--name local-m5-*`) is holding the
  session lock but its own connection went stale. Reattaching requires killing
  that pool server first. That is a **live process on the boss's box** — ask
  before killing, and check `worker=idle` first so nothing is in flight. The
  supervisor generally respawns the pool server afterwards.

Do not kill a pool server to "clean up" a session that is already connected.

### 5. Mass simultaneous death = host signed out

If bridges reattach fine, run for hours, then **all die within a second or two
of each other**, the cause is not the bridges — the host's Claude Code
credentials were invalidated. Signature in every log:

```
[01:27:39] Session failed: Process exited with error <cse_id>
```

The transport connects, but the inner `claude` worker cannot authenticate and
exits, so the session flips back to `conn=disconnected` and the app shows
**"Your computer needs to sign in again"**. Confirm by correlating death times
across logs and checking when the credential store was last written:

```bash
grep -ahoE "\[[0-9:]+\] Session failed" /tmp/reattach-*.log | sort | uniq -c
security find-generic-password -s "Claude Code-credentials" 2>&1 | grep mdat
```

Deaths clustered in one second + a `mdat` older than the deaths = signed out.
**Never print or copy the credential value — metadata only.**

Fix: the boss signs in again in a terminal (`claude` on the host; the app will
not prompt for this and cannot do it for them — only they can complete the
sign-in). That rewrites the Keychain entry; verify `mdat` is now recent. The
already-dead bridges do **not** pick the new token up on their own — rerun the
whole sweep from step 1. Reattach one session first and give it ~60s to prove
it holds before batch-spawning the rest, so a still-bad token costs one process
instead of a dozen.

## Laptop sleep — the recurring cause, and the cron fix

On a **notebook** this is not a one-off. Every lid-close / network change
severs all bridges at once, and they never come back on their own; on wake the
CLI refreshes its token, which is why the app blames a sign-out. The tell is
the same as above (deaths clustered in one second) — check `pmset -g log` and
ask where the machine has been before concluding anything about credentials.

A sleep-killed bridge also **releases any session lock it held**, so the
"already being served by pid N" cases from step 4 tend to resolve themselves
after a sleep cycle. Wait one out before considering a kill.

Rather than hand-sweeping after every commute, run the unattended sweeper:

```bash
~/dev/ai-harness/skills/revive-bridges/scripts/auto_revive.sh --dry-run
```

It only ever *adds* a transport — never kills, archives, or runs `takeover` —
skips any cwd outside `$HOME` (other host) or already served, and applies a
per-session backoff (3 failures/hour) so a real outage cannot turn into a spawn
storm. Install it as a LaunchAgent (5-minute interval; launchd fires it shortly
after wake, which is the case that matters):

```bash
cp ~/dev/ai-harness/skills/revive-bridges/scripts/com.dtogni.claude-bridge-revive.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.dtogni.claude-bridge-revive.plist
```

Log: `~/.ai-harness/auto-revive/auto-revive.log`. To stop it:
`launchctl bootout gui/$UID/com.dtogni.claude-bridge-revive`.

## Fallback (only when reattach truly errors)

```bash
cd ~/dev/ai-harness
RC_NO_CROSS_SESSION_SETTINGS=1 python3 -m remote_control relaunch \
    --from <cse_id> --max-turns 10 --wait-timeout 120 --force
```

`RC_NO_CROSS_SESSION_SETTINGS=1` is **required** — without it the spawn dies
with `unknown option '--name'` (the top-level `--settings` flag placed before
the `remote-control` subcommand makes claude 2.1.2xx reject the following
`--name`; documented at `remote_control/new_session.py:206`).

Taking this path mints a new `cse_*`, so afterwards re-point the manager
roster: `~/.ai-harness/manager/<old_cse>.jsonl` does not follow the new id, and
every worker spawned with `--reply-to <old_cse>` still reports to the dead one.

## Report back

Which sessions reconnected, which needed the fallback (with their new
`cse_*`), and which stayed dead with the log output that explains why.

## Related

- [[list-sessions]] — friendlier inventory of what is live
- [[relaunch]] / [[takeover]] — fresh-session fallbacks, not first resort
- [[session-triage]] — for sessions that died mid-task, not mid-transport
