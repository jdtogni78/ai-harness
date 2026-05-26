#!/usr/bin/env bash
# Safely restart the launchd-managed remote-control supervisor on THIS host,
# preserving the work that lives in the cse_ sessions its mm-* servers own.
#
# Why: bare `launchctl kickstart -k` TERMs every mm-* server, which kills the
# cse_ bridge sessions they own. The next supervisor incarnation spawns fresh
# mm-* servers with NEW cse_ ids, so the old cse_s become disconnected forever
# unless we fork their on-disk transcripts to local siblings first. Composes:
#
#   1. snapshot the active this-host cse_ ids (via `sessions --location this-host`)
#   2. launchctl kickstart -k <label>
#   3. wait for the supervisor to spawn fresh servers (poll mm-*.log mtimes)
#   4. fork-all the snapshotted ids (now-disconnected, transcripts still on disk)
#   5. (optional --archive) archive each source after its fork succeeds
#   6. (optional --open-app) surface Claude.app at the cwd so the user can pick
#      a fork from /resume; otherwise just print the resume commands.
#
# This is a composer over the existing primitives -- it adds no new behavior
# beyond the snapshot+wait+invoke pattern. Each step is the same command you
# could run by hand, so a partial run leaves a clearly diagnosable state.

set -euo pipefail

# --- args ---------------------------------------------------------------------
ARCHIVE=0
OPEN_APP=0
DRY_RUN=0
WAIT_SECS=45        # max time we'll wait for fresh servers post-kickstart
GRACE_SECS=10       # extra grace after detection so the supervisor settles
LABEL=""            # launchd label; auto-derived if empty
usage() {
  cat <<EOF
usage: $0 [--archive] [--open-app] [--dry-run] [--label LBL]
         [--wait SECS] [--grace SECS]
  --archive   : after each successful fork, POST /sessions/{id}/archive (clears
                the orphan from the cloud session list).
  --open-app  : after forking, \`open -a Claude.app <repo>\` so the desktop
                /resume picker shows the new local forks.
  --dry-run   : take the snapshot, print what WOULD happen, do NOT kickstart,
                fork, or archive. Safe to run anytime.
  --label LBL : override the launchd label (default: auto-detect the loaded
                com.*.claude-remote-control label via \`launchctl list\`).
  --wait SECS : max seconds to wait for fresh servers (default $WAIT_SECS).
  --grace SECS: extra settle time after fresh servers appear (default $GRACE_SECS).
EOF
}
while [ $# -gt 0 ]; do
  case "$1" in
    --archive) ARCHIVE=1 ;;
    --open-app) OPEN_APP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --label) shift; LABEL="$1" ;;
    --wait) shift; WAIT_SECS="$1" ;;
    --grace) shift; GRACE_SECS="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- helpers ------------------------------------------------------------------
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

detect_label() {
  # Pick the single com.*.claude-remote-control label currently loaded in our
  # gui domain. Fail loudly if zero or multiple match -- the user must say.
  local found
  found=$(launchctl list 2>/dev/null | awk '/com\.[a-z0-9]+\.claude-remote-control$/{print $3}')
  local n; n=$(printf '%s\n' "$found" | grep -c . || true)
  if [ "$n" -ne 1 ]; then
    echo "could not auto-detect launchd label (found $n match(es)); pass --label" >&2
    [ -n "$found" ] && printf '  candidate: %s\n' $found >&2
    return 1
  fi
  printf '%s' "$found"
}

# --- 1. snapshot --------------------------------------------------------------
log "snapshot: active this-host cse_ ids"
SNAPSHOT_FILE=$(mktemp -t supervisor-restart.XXXXXX)
trap 'rm -f "$SNAPSHOT_FILE"' EXIT
python3 -m remote_control sessions --location this-host --ids-only > "$SNAPSHOT_FILE"
SNAP_COUNT=$(wc -l < "$SNAPSHOT_FILE" | tr -d ' ')
log "snapshot: $SNAP_COUNT cse_ id(s) on this host"
if [ "$SNAP_COUNT" -gt 0 ]; then
  sed 's/^/  - /' "$SNAPSHOT_FILE"
fi

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run: would now kickstart, wait, and fork-all the $SNAP_COUNT id(s)"
  log "dry-run: fork-all preview ↓"
  if [ "$SNAP_COUNT" -gt 0 ]; then
    python3 -m remote_control fork-all --ids $(cat "$SNAPSHOT_FILE") --dry-run
  fi
  exit 0
fi

# --- 2. kickstart -------------------------------------------------------------
if [ -z "$LABEL" ]; then
  LABEL=$(detect_label) || exit 2
fi
log "kickstart: gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

# --- 3. wait for fresh servers ------------------------------------------------
# The supervisor logs a "start: mm-X" line to logs/manager.log when it spawns
# a server. We poll for at least one such line dated AFTER the kickstart.
KICK_EPOCH=$(date +%s)
LOGDIR="$REPO_ROOT/logs"
log "waiting up to ${WAIT_SECS}s for the supervisor to spawn fresh mm-* servers"
deadline=$(( KICK_EPOCH + WAIT_SECS ))
while :; do
  now=$(date +%s)
  if [ "$now" -ge "$deadline" ]; then
    log "WARN: timed out waiting for fresh servers; proceeding with fork-all anyway"
    break
  fi
  # New "start: mm-..." lines in manager.log dated > KICK_EPOCH?
  manager_log_mtime=$(stat -f %m "$LOGDIR/manager.log" 2>/dev/null || echo 0)
  if [ "$manager_log_mtime" -gt "$KICK_EPOCH" ] \
     && tail -50 "$LOGDIR/manager.log" 2>/dev/null | grep -q "start: mm-"; then
    log "supervisor is spawning fresh servers"
    sleep "$GRACE_SECS"
    break
  fi
  sleep 1
done

# --- 4. fork-all the snapshotted ids -----------------------------------------
if [ "$SNAP_COUNT" -eq 0 ]; then
  log "nothing to fork (snapshot was empty)"
  exit 0
fi
ARCHIVE_FLAG=""
[ "$ARCHIVE" = 1 ] && ARCHIVE_FLAG="--archive"
log "fork-all: $SNAP_COUNT id(s) --into-main $ARCHIVE_FLAG"
# We capture the output so we can grep new uuids out for step 6. fork-all's
# stdout is "forked <cse> -> <uuid>" per line plus a trailing summary.
FORK_OUT=$(mktemp -t fork-all.XXXXXX)
trap 'rm -f "$SNAPSHOT_FILE" "$FORK_OUT"' EXIT
if ! python3 -m remote_control fork-all --ids $(cat "$SNAPSHOT_FILE") \
       --into-main $ARCHIVE_FLAG | tee "$FORK_OUT"; then
  log "WARN: fork-all reported failures; see output above"
fi

NEW_UUIDS=$(awk '/^forked .* -> /{print $4}' "$FORK_OUT")
NEW_COUNT=$(printf '%s\n' "$NEW_UUIDS" | grep -c . || true)
log "forks produced: $NEW_COUNT local uuid(s)"
[ "$NEW_COUNT" -eq 0 ] && exit 1

# --- 5/6. surface for resume --------------------------------------------------
if [ "$OPEN_APP" = 1 ]; then
  log "opening Claude.app at $REPO_ROOT for /resume picker"
  python3 -m remote_control resume $NEW_UUIDS --open-app
else
  echo
  log "to resume any fork (or paste them into Claude.app /resume):"
  python3 -m remote_control resume $NEW_UUIDS
fi
