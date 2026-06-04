#!/bin/bash
# workers.sh — manager-side worker tracking (per-manager JSONL state log)
#
# A single manager session may dispatch several worker sessions across the
# course of its life. This script is the manager's notebook: an append-only
# JSONL log of dispatch / status / close events, folded into a current-state
# view on read. One state file per manager (keyed by the manager's own cse_*).
#
# Subcommands:
#   register <worker_id> <dir> [--ticket N] [--brief TEXT]
#   update   <worker_id> [--status S] [--note TEXT]
#   close    <worker_id> [--reason TEXT]
#   forget   <worker_id> [--reason TEXT]
#   list     [--all] [--json]    (default: active only, table form)
#   get      <worker_id>         (latest folded record as JSON)
#   path                         (print state file path)
#
# Manager id resolution order:
#   1. $MANAGER_CSE_ID (explicit override)
#   2. JWT in $CLAUDE_CODE_SESSION_ACCESS_TOKEN (the desktop app injects this
#      into every spawned Claude Code process; same path new-session uses).
#
# State dir: $MANAGER_STATE_DIR (default ~/.ai-harness/manager).

set -euo pipefail

STATE_DIR="${MANAGER_STATE_DIR:-$HOME/.ai-harness/manager}"
AI_HARNESS_DIR="${AI_HARNESS_DIR:-$HOME/dev/ai-harness}"

die() { echo "workers.sh: $*" >&2; exit 2; }

require_jq() { command -v jq >/dev/null 2>&1 || die "jq is required"; }

resolve_manager_id() {
  if [[ -n "${MANAGER_CSE_ID:-}" ]]; then
    printf '%s\n' "$MANAGER_CSE_ID"
    return 0
  fi
  AI_HARNESS_DIR="$AI_HARNESS_DIR" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["AI_HARNESS_DIR"])
try:
    from remote_control.session_list import own_session_id_from_env
except Exception as e:
    sys.stderr.write(f"workers.sh: cannot import ai-harness helper: {e}\n")
    sys.exit(2)
sid = own_session_id_from_env(dict(os.environ))
if not sid:
    sys.stderr.write(
        "workers.sh: could not resolve own cse_id "
        "(set MANAGER_CSE_ID or run inside a Claude Code session)\n"
    )
    sys.exit(3)
print(sid)
PY
}

state_path() {
  local mgr
  mgr="$(resolve_manager_id)" || return $?
  mkdir -p "$STATE_DIR" >/dev/null
  chmod 700 "$STATE_DIR" 2>/dev/null || true
  printf '%s/%s.jsonl\n' "$STATE_DIR" "$mgr"
}

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

append_event() {
  local file
  file="$(state_path)" || return $?
  printf '%s\n' "$1" >> "$file"
}

cmd_register() {
  [[ $# -ge 2 ]] || die "register: need <worker_id> <dir>"
  local worker="$1"; shift
  local dir="$1"; shift
  local ticket="" brief=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ticket) ticket="${2:-}"; shift 2 ;;
      --brief)  brief="${2:-}";  shift 2 ;;
      *) die "register: unknown arg: $1" ;;
    esac
  done
  require_jq
  local rec
  rec="$(jq -nc \
    --arg ev "register" --arg w "$worker" --arg d "$dir" \
    --arg t "$ticket" --arg b "$brief" --arg ts "$(now_utc)" \
    '{event:$ev, worker:$w, dir:$d, ticket:$t, brief:$b, ts:$ts}')"
  append_event "$rec"
  printf 'registered %s\n' "$worker"
}

cmd_update() {
  [[ $# -ge 1 ]] || die "update: need <worker_id>"
  local worker="$1"; shift
  local status="" note=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status) status="${2:-}"; shift 2 ;;
      --note)   note="${2:-}";   shift 2 ;;
      *) die "update: unknown arg: $1" ;;
    esac
  done
  require_jq
  local rec
  rec="$(jq -nc \
    --arg ev "update" --arg w "$worker" --arg s "$status" --arg n "$note" \
    --arg ts "$(now_utc)" \
    '{event:$ev, worker:$w, status:$s, note:$n, ts:$ts}')"
  append_event "$rec"
  printf 'updated %s\n' "$worker"
}

cmd_close() {
  [[ $# -ge 1 ]] || die "close: need <worker_id>"
  local worker="$1"; shift
  local reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason) reason="${2:-}"; shift 2 ;;
      *) die "close: unknown arg: $1" ;;
    esac
  done
  require_jq
  local rec
  rec="$(jq -nc \
    --arg ev "close" --arg w "$worker" --arg r "$reason" --arg ts "$(now_utc)" \
    '{event:$ev, worker:$w, reason:$r, ts:$ts}')"
  append_event "$rec"
  printf 'closed %s\n' "$worker"
}

cmd_forget() {
  [[ $# -ge 1 ]] || die "forget: need <worker_id>"
  local worker="$1"; shift
  local reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason) reason="${2:-}"; shift 2 ;;
      *) die "forget: unknown arg: $1" ;;
    esac
  done
  require_jq
  local rec
  rec="$(jq -nc \
    --arg ev "forget" --arg w "$worker" --arg r "$reason" --arg ts "$(now_utc)" \
    '{event:$ev, worker:$w, reason:$r, ts:$ts}')"
  append_event "$rec"
  printf 'forgot %s\n' "$worker"
}

cmd_list() {
  local include_all=false as_json=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)  include_all=true; shift ;;
      --json) as_json=true;     shift ;;
      *) die "list: unknown arg: $1" ;;
    esac
  done
  require_jq
  local file
  file="$(state_path)" || return $?
  if [[ ! -s "$file" ]]; then
    if $as_json; then echo "[]"; else echo "(no workers tracked)"; fi
    return 0
  fi

  local folded
  folded="$(jq -s '
    reduce .[] as $e ({};
      if $e.event == "register" then
        .[$e.worker] = {
          worker: $e.worker, dir: $e.dir, ticket: $e.ticket, brief: $e.brief,
          status: "spawned", note: "", spawned_at: $e.ts, last_ts: $e.ts,
          state: "active"
        }
      elif $e.event == "update" then
        (.[$e.worker] // {worker:$e.worker, state:"active"}) as $cur
        | .[$e.worker] = ($cur
            + (if $e.status != "" then {status:$e.status} else {} end)
            + (if $e.note   != "" then {note:$e.note}     else {} end)
            + {last_ts: $e.ts})
      elif $e.event == "close" then
        .[$e.worker] = ((.[$e.worker] // {worker:$e.worker})
          + {state:"closed", reason:$e.reason, last_ts:$e.ts})
      elif $e.event == "forget" then
        .[$e.worker] = ((.[$e.worker] // {worker:$e.worker})
          + {state:"forgotten", reason:$e.reason, last_ts:$e.ts})
      else . end
    )
    | [.[]]
    | sort_by(.last_ts)
  ' "$file")"

  if ! $include_all; then
    folded="$(echo "$folded" | jq '[ .[] | select(.state == "active") ]')"
  fi

  if $as_json; then
    echo "$folded"
    return 0
  fi

  local count
  count="$(echo "$folded" | jq 'length')"
  if [[ "$count" -eq 0 ]]; then
    if $include_all; then echo "(no workers tracked)"
    else echo "(no active workers — try --all)"; fi
    return 0
  fi
  echo "$folded" | jq -r '.[] |
    "  \(.worker)   [\(.state)]   ticket=\(.ticket // "-")   dir=\(.dir // "-")\n      status: \(.status // "?")\n      note:   \(.note // "")\n      brief:  \(.brief // "")\n      last:   \(.last_ts // "?")"'
}

cmd_get() {
  [[ $# -ge 1 ]] || die "get: need <worker_id>"
  local worker="$1"
  require_jq
  cmd_list --all --json | jq --arg w "$worker" '.[] | select(.worker == $w)'
}

cmd_path() { state_path; }

loop_marker() {
  local mgr
  mgr="$(resolve_manager_id)" || return $?
  mkdir -p "$STATE_DIR" >/dev/null
  printf '%s/%s.loop\n' "$STATE_DIR" "$mgr"
}

cmd_loop_state() {
  local marker
  marker="$(loop_marker)" || return $?
  if [[ -f "$marker" ]]; then
    printf 'armed\n'
    cat "$marker"
  else
    printf 'disarmed\n'
  fi
}

cmd_loop_arm() {
  local marker
  marker="$(loop_marker)" || return $?
  printf 'armed_at=%s\n' "$(now_utc)" > "$marker"
  printf 'loop armed\n'
}

cmd_loop_disarm() {
  local marker
  marker="$(loop_marker)" || return $?
  rm -f "$marker"
  printf 'loop disarmed\n'
}

usage() {
  cat <<'EOF'
workers.sh — manager-side worker tracking (per-manager JSONL state log)

Subcommands:
  register <worker_id> <dir> [--ticket N] [--brief TEXT]
  update   <worker_id> [--status S] [--note TEXT]
  close    <worker_id> [--reason TEXT]
  forget   <worker_id> [--reason TEXT]
  list     [--all] [--json]    (default: active only, table form)
  get      <worker_id>         (latest folded record as JSON)
  path                         (print state file path)
  loop-state                   (print 'armed' [+ armed_at] or 'disarmed')
  loop-arm                     (set the loop-armed marker for this manager)
  loop-disarm                  (clear the loop-armed marker)

Env:
  MANAGER_CSE_ID      override manager id (else from CLAUDE_CODE_SESSION_ACCESS_TOKEN JWT)
  MANAGER_STATE_DIR   override state dir (default ~/.ai-harness/manager)
  AI_HARNESS_DIR      override ai-harness checkout location (default ~/dev/ai-harness)
EOF
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    register)    cmd_register    "$@" ;;
    update)      cmd_update      "$@" ;;
    close)       cmd_close       "$@" ;;
    forget)      cmd_forget      "$@" ;;
    list)        cmd_list        "$@" ;;
    get)         cmd_get         "$@" ;;
    path)        cmd_path        "$@" ;;
    loop-state)  cmd_loop_state  "$@" ;;
    loop-arm)    cmd_loop_arm    "$@" ;;
    loop-disarm) cmd_loop_disarm "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown subcommand: $sub" ;;
  esac
}

main "$@"
