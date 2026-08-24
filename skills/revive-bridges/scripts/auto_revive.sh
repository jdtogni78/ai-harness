#!/usr/bin/env bash
# auto_revive.sh — reattach LOCAL bridge sessions that have gone
# conn=disconnected (laptop sleep, network change, token refresh).
#
# Safety contract — this runs unattended, so it only ever ADDS a transport:
#   * never kills a process, never runs `takeover`, never archives a session
#   * never touches a session already served (stale-lock error is a no-op)
#   * skips anything whose cwd is missing or outside $HOME (other host)
#   * per-session backoff so a real outage can't become a spawn storm
#   * single-instance via flock; hard cap on spawns per run
#
# Usage: auto_revive.sh [--dry-run] [--max N]
set -uo pipefail

HARNESS="${HARNESS:-$HOME/dev/ai-harness}"
STATE="$HOME/.ai-harness/auto-revive"
LOG="$STATE/auto-revive.log"
LOCK="$STATE/.lock.d"
MAX_SPAWNS="${MAX_SPAWNS:-8}"     # cap per run
FAIL_WINDOW=3600                  # backoff window, seconds
FAIL_LIMIT=3                      # give up after N failures in the window

DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --max) shift; MAX_SPAWNS="$1" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$STATE"

# Single-instance lock. macOS has no flock(1), so use an atomic mkdir and
# clear the lock if its owner PID is gone (crashed run must not wedge cron).
if ! mkdir "$LOCK" 2>/dev/null; then
  owner=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    echo "$(date '+%F %T') another run (pid $owner) in progress, skipping" >>"$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') clearing stale lock (pid ${owner:-unknown})" >>"$LOG"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { echo "$(date '+%F %T') lock race, skipping" >>"$LOG"; exit 0; }
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }
[ "$DRY" = 1 ] && log() { echo "$(date '+%F %T') $*"; }

cd "$HARNESS" || { log "FATAL: no $HARNESS"; exit 1; }

now=$(date +%s)

# --- backoff bookkeeping -------------------------------------------------
fail_count() {  # cse_id -> failures inside the window
  local f="$STATE/fail-$1"
  [ -f "$f" ] || { echo 0; return; }
  awk -v now="$now" -v w="$FAIL_WINDOW" '$1 > now-w' "$f" | wc -l | tr -d ' '
}
record_fail() {
  local f="$STATE/fail-$1"
  echo "$now" >>"$f"
  awk -v now="$now" -v w="$FAIL_WINDOW" '$1 > now-w' "$f" >"$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
}
clear_fail() { rm -f "$STATE/fail-$1"; }

# --- collect disconnected sessions --------------------------------------
listing=$(python3 -m remote_control sessions 2>/dev/null) || { log "FATAL: sessions call failed"; exit 1; }

disconnected=$(echo "$listing" | awk '
  /^cse_/            { id=$1 }
  /conn=disconnected/{ if (id != "") { print id; id="" } }
')

[ -z "$disconnected" ] && { log "nothing disconnected"; exit 0; }

spawned=0
attempted=""
for sid in $disconnected; do
  [ "$spawned" -ge "$MAX_SPAWNS" ] && { log "hit MAX_SPAWNS=$MAX_SPAWNS, stopping (more remain)"; break; }

  # already have a transport for it? then it is mid-handshake, leave alone.
  if pgrep -f -- "--session-id $sid" >/dev/null 2>&1; then
    log "SKIP $sid — transport already running"
    continue
  fi

  n=$(fail_count "$sid")
  if [ "$n" -ge "$FAIL_LIMIT" ]; then
    log "SKIP $sid — backoff ($n failures in last $((FAIL_WINDOW/60))m)"
    continue
  fi

  cwd=$(python3 -m remote_control relaunch --from "$sid" --dry-run --show-brief 2>/dev/null \
        | awk '/^  cwd/ { print $3; exit }')

  case "$cwd" in
    "")        log "SKIP $sid — cwd unresolved";               record_fail "$sid"; continue ;;
    "$HOME"/*) : ;;
    *)         log "SKIP $sid — cwd '$cwd' not under \$HOME (other host)"; continue ;;
  esac
  [ -d "$cwd" ] || { log "SKIP $sid — cwd '$cwd' is gone"; continue; }

  if [ "$DRY" = 1 ]; then
    log "WOULD reattach $sid in $cwd"
    spawned=$((spawned+1))
    continue
  fi

  out="$STATE/reattach-${sid:4:8}.log"
  : >"$out"
  ( cd "$cwd" && nohup claude remote-control --session-id "$sid" \
        --permission-mode bypassPermissions >"$out" 2>&1 & )
  log "reattach $sid in $cwd"
  attempted="$attempted $sid"
  spawned=$((spawned+1))
done

[ "$DRY" = 1 ] && { log "dry-run: $spawned would be reattached"; exit 0; }
[ "$spawned" = 0 ] && exit 0

# --- verify: give the handshakes time, then score the result -------------
sleep 45
after=$(python3 -m remote_control sessions 2>/dev/null)
for sid in $attempted; do
  st=$(echo "$after" | awk -v id="$sid" '$1==id {found=1} found && /conn=/ {print; exit}')
  case "$st" in
    *conn=connected*) log "OK   $sid"; clear_fail "$sid" ;;
    *)                log "FAIL $sid — $(tail -c 200 "$STATE/reattach-${sid:4:8}.log" 2>/dev/null | tr -d '\r' | tr '\n' ' ' | tail -c 120)"
                      record_fail "$sid" ;;
  esac
done
log "run complete: $spawned attempted"
