---
name: remote-control-dirs
description: >-
  Manage the allowlist of dev dirs that the ai-harness supervisor
  spawns `claude remote-control` servers for. Use when the user wants to
  "enable/disable" a dir for remote control, asks "which dirs are active for
  remote control", "stop spawning so many servers", "add <repo> to remote
  control", "remove <repo> from remote control", "show remote-control status",
  or wants to edit the activation list. The list lives at
  `/Users/user/dev/ai-harness/active-dirs.txt` and the
  supervisor re-reads it every ~30s.
---

# Manage the remote-control activation list

The launchd-managed supervisor in `/Users/user/dev/ai-harness/`
spawns one `claude remote-control` server per dir listed in
**`active-dirs.txt`** (one basename per line). It reloads the file every
`TICK_SECS` (~30s) — so edits take effect within a tick, no restart needed.

- `dev` is the special entry for the `~/dev` root itself (server name
  `<host>-dev`, where `<host>` is the supervisor's host nickname).
- Every other entry is a basename under `~/dev` (e.g. `my-app` →
  `<host>-my-app`).
- Lines starting with `#` and blank lines are ignored.
- **Fail-closed:** if the file is missing, the supervisor allows nothing.
- **Removing an entry sends SIGTERM** to its server within ~30s — even if it
  has an active session. Warn the user before removing if a session is busy.

## Variables

```
ACTIVE_FILE=/Users/user/dev/ai-harness/active-dirs.txt
DEV=/Users/user/dev
LOGDIR=/Users/user/dev/ai-harness/logs
```

## Status (the common ask: "what's active?")

Show three columns: **allowlisted**, **running**, **dir-on-disk**. Discrepancies
matter — e.g. allowlisted but not running = supervisor hasn't ticked yet or the
dir is missing; running but not allowlisted = will be deactivated next tick.

```bash
# Allowlisted (parsed)
grep -vE '^[[:space:]]*(#|$)' "$ACTIVE_FILE" | awk '{$1=$1;print}'

# Currently running (uses ps because macOS pgrep -a is a no-op). The --name
# token is "<host>-<basename>"; we don't filter by host here because there's
# only ever one host's supervisor on this machine.
ps -axo command | awk '/\/claude[ ]+remote-control[ ]+--name[ ]+/ {
  for (i=1;i<=NF;i++) if ($i=="--name") { print $(i+1); break } }' | sort -u

# Subdirs on disk
ls -1 "$DEV"
```

Report as a small table; call out anything in "running but not allowlisted"
(pending deactivation) or "allowlisted but no dir on disk" (typo / stale entry).

## Enable a dir

Add the basename (idempotent — don't double-add).

```bash
# Replace <name> with the basename. Use `dev` for the root itself.
name=<name>
grep -qxE "^[[:space:]]*${name}[[:space:]]*$" "$ACTIVE_FILE" \
  || printf '%s\n' "$name" >> "$ACTIVE_FILE"
```

Then `tail -20 "$LOGDIR/manager.log"` after ~30s to confirm `start: <host>-<name>`.

## Disable a dir

Strip the line cleanly (preserve comments). Confirm with the user first if the
server currently shows `Capacity >= 1` (busy with a live session) — disabling
will kill that session.

```bash
name=<name>
# Check if busy before destructive action. The log file is
# "<host>-<name>.log" -- glob so we don't need to know the host nickname.
tail -c 200000 "$LOGDIR/"*-"${name}.log" 2>/dev/null \
  | grep -aoE 'Capacity: [0-9]+/[0-9]+' | tail -1

# Remove (uses a tmpfile so the supervisor never sees a half-written file):
tmp=$(mktemp) && awk -v n="$name" '
  { line=$0; sub(/#.*/,"",line); gsub(/[[:space:]]/,"",line);
    if (line==n) next; print }
' "$ACTIVE_FILE" > "$tmp" && mv "$tmp" "$ACTIVE_FILE"
```

Then `tail -20 "$LOGDIR/manager.log"` after ~30s to confirm
`deactivate: <host>-<name> not in allowlist`.

## Reload now (rarely needed)

The supervisor reloads on its own tick. If the user is impatient:

```bash
# Preserves any work in the currently-running cse_ sessions (snapshots them,
# kickstarts, waits for fresh servers, then fork-alls the orphans to local
# siblings the user can /resume in Claude.app).
scripts/supervisor-restart.sh                # safe restart, no archive/open
scripts/supervisor-restart.sh --dry-run      # show what would happen, do nothing
scripts/supervisor-restart.sh --archive      # also archive each source after fork succeeds
scripts/supervisor-restart.sh --open-app     # also surface Claude.app at the repo cwd
```

Composable primitives (each runnable on its own — the script just chains them):

```bash
# Find stale this-host bridge sessions for any repo:
python3 -m remote_control sessions --stale --location this-host --ids-only

# Bulk-fork a set of cse_ ids (per-id failure-tolerant; --archive after success):
python3 -m remote_control fork-all --ids cse_a cse_b … --into-main [--archive]

# Surface local forks: print the resume commands; optionally open Claude.app:
python3 -m remote_control resume <uuid>… [--open-app] [--open-terminal]

# Archive cse_ session(s) directly (POST /sessions/{id}/archive):
python3 -m remote_control sessions archive cse_a cse_b…
```

Bare `launchctl kickstart -k …` still works but **orphans the running cse_s**:
their `<host>-*` servers get TERMed and never reconnect; the next supervisor
cycle opens fresh `cse_`s. Use the script above unless you genuinely want to
discard the in-flight sessions.

## Don't

- Don't edit `remote-control-supervisor.sh` for routine enable/disable — that's
  what this list is for.
- Don't `pkill` a `<host>-*` server to "disable" it; the supervisor will
  respawn it on the next tick. Edit the allowlist instead.
- Don't `> active-dirs.txt` (truncate) unless you mean "deactivate everything";
  the supervisor fails-closed and will TERM all servers.
- Don't bare-`launchctl kickstart -k` the supervisor when sessions are live —
  use `scripts/supervisor-restart.sh` instead so the cse_ transcripts get
  forked to local siblings before the kickstart cuts them off.
