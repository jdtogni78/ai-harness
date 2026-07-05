#!/bin/bash
# workers.sh — manager-side worker tracking (per-manager JSONL state log)
#
# A single manager session may dispatch several worker sessions across the
# course of its life. This script is the manager's notebook: an append-only
# JSONL log of dispatch / status / close events, folded into a current-state
# view on read. One state file per manager (keyed by the manager's own cse_*).
#
# Subcommands:
#   register <worker_id> <dir> --ticket N [--brief TEXT]
#   update   <worker_id> [--status S] [--note TEXT]
#   close    <worker_id> [--reason TEXT]
#   forget   <worker_id> [--reason TEXT]
#   list     [--all] [--json]    (default: active only, table form)
#   get      <worker_id>         (latest folded record as JSON)
#   path                         (print state file path)
#   mgr-id                       (allocate / read this manager's host-scoped ordinal)
#   mgr-cwd                      (read the cwd snapshotted on mgr-id's first call)
#   retitle  "<task>"            (compose `titles set` for the manager title)
#   retitle-worker <wid> [brief] (compose `titles set` for one worker title)
#   migrate-titles [--apply]     (rewrite legacy [MGR-N] manager titles; --apply
#                                 to PUT, else dry-run plan)
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
  # Mandatory ticket per #88: a child without a tracking ticket is a bug.
  # Accept bare digits or a leading '#'.
  ticket="${ticket#\#}"
  [[ "$ticket" =~ ^[0-9]+$ ]] || die "register: --ticket <N> is required (got: '${ticket:-empty}')"
  require_jq
  # Allocate next worker_ord per manager: count of past register events + 1.
  # Non-recycling — re-registering an existing worker_id keeps its ord.
  local file existing_ord worker_ord
  file="$(state_path)" || return $?
  if [[ -s "$file" ]]; then
    existing_ord="$(jq -r --arg w "$worker" '
      [ .[] | select(.event == "register" and .worker == $w) ][0] | .worker_ord // empty
    ' < <(jq -s '.' "$file"))"
  else
    existing_ord=""
  fi
  if [[ -n "$existing_ord" ]]; then
    worker_ord="$existing_ord"
  else
    local prior
    if [[ -s "$file" ]]; then
      prior="$(jq -s '[ .[] | select(.event == "register") | .worker ] | unique | length' "$file")"
    else
      prior=0
    fi
    worker_ord=$((prior + 1))
  fi
  local rec
  rec="$(jq -nc \
    --arg ev "register" --arg w "$worker" --arg d "$dir" \
    --arg t "$ticket" --arg b "$brief" --arg ts "$(now_utc)" \
    --argjson ord "$worker_ord" \
    '{event:$ev, worker:$w, dir:$d, ticket:$t, brief:$b, worker_ord:$ord, ts:$ts}')"
  append_event "$rec"
  printf 'registered %s (worker_ord=%s, ticket=#%s)\n' "$worker" "$worker_ord" "$ticket"
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
          worker_ord: ($e.worker_ord // null),
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
    "  \(.worker)   [\(.state)]   W\(.worker_ord // "?")   ticket=#\(.ticket // "-")   dir=\(.dir // "-")\n      status: \(.status // "?")\n      note:   \(.note // "")\n      brief:  \(.brief // "")\n      last:   \(.last_ts // "?")"'
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

ordinals_path() {
  mkdir -p "$STATE_DIR" >/dev/null
  printf '%s/ordinals.jsonl\n' "$STATE_DIR"
}

# Read this manager's record from ordinals.jsonl as JSON, or empty string.
_ordinal_record() {
  local mgr file
  mgr="$(resolve_manager_id)" || return $?
  file="$(ordinals_path)" || return $?
  [[ -s "$file" ]] || { printf ''; return 0; }
  jq -s -c --arg m "$mgr" '[ .[] | select(.cse_id == $m) ][0] // empty' "$file"
}

cmd_mgr_id() {
  require_jq
  local mgr file existing rec ord prior
  mgr="$(resolve_manager_id)" || return $?
  file="$(ordinals_path)" || return $?
  existing="$(_ordinal_record)" || return $?
  if [[ -n "$existing" ]]; then
    printf '%s\n' "$(echo "$existing" | jq -r '.ord')"
    return 0
  fi
  # Allocate next ordinal as max(existing.ord) + 1, host-scoped via the state
  # file's lifetime (per-machine since the state dir is local).
  if [[ -s "$file" ]]; then
    prior="$(jq -s '[ .[] | .ord ] | (max // 0)' "$file")"
  else
    prior=0
  fi
  ord=$((prior + 1))
  rec="$(jq -nc \
    --arg m "$mgr" --argjson o "$ord" \
    --arg cwd "$PWD" --arg ts "$(now_utc)" \
    '{cse_id:$m, ord:$o, cwd:$cwd, allocated_at:$ts}')"
  printf '%s\n' "$rec" >> "$file"
  printf '%s\n' "$ord"
}

cmd_mgr_cwd() {
  require_jq
  local existing
  existing="$(_ordinal_record)" || return $?
  if [[ -z "$existing" ]]; then
    die "mgr-cwd: no ordinal allocated for this manager yet (call mgr-id first)"
  fi
  echo "$existing" | jq -r '.cwd'
}

# Compose & invoke `titles set` for the manager's own title.
cmd_retitle() {
  [[ $# -ge 1 ]] || die "retitle: need <task-description>"
  local task="$1"
  require_jq
  local mgr ord mgr_cwd count plural
  mgr="$(resolve_manager_id)" || return $?
  # Auto-allocate the ordinal on first retitle if mgr-id was never called.
  ord="$(cmd_mgr_id)" || return $?
  mgr_cwd="$(cmd_mgr_cwd)" || return $?
  count="$(cmd_list --json | jq 'length')"
  plural="s"; [[ "$count" == "1" ]] && plural=""
  ( cd "$AI_HARNESS_DIR" && \
    python3 -m remote_control titles set \
      --id "$mgr" --cwd "$mgr_cwd" \
      --sub "MGR-${ord}" \
      "${task} (${count} worker${plural})" )
}

# Compose & invoke `titles set` for one worker. Reads worker's dir, worker_ord,
# ticket from the state log; emits the chained
#   [NICK.host][MGR<ord>-W<k>][#<ticket>] <brief>
# form. Second positional arg overrides the stored brief.
cmd_retitle_worker() {
  [[ $# -ge 1 ]] || die "retitle-worker: need <worker_id> [brief]"
  local worker="$1"; shift
  local override_brief="${1:-}"
  require_jq
  local rec ord wdir wk ticket brief
  ord="$(cmd_mgr_id)" || return $?
  rec="$(cmd_get "$worker")"
  [[ -n "$rec" && "$rec" != "null" ]] || die "retitle-worker: unknown worker $worker"
  wdir="$(echo "$rec" | jq -r '.dir // empty')"
  wk="$(echo "$rec" | jq -r '.worker_ord // empty')"
  ticket="$(echo "$rec" | jq -r '.ticket // empty')"
  brief="$(echo "$rec" | jq -r '.brief // empty')"
  [[ -n "$wdir" ]] || die "retitle-worker: $worker has no dir recorded"
  [[ -n "$wk" ]]   || die "retitle-worker: $worker has no worker_ord (re-register against the new register?)"
  [[ -n "$override_brief" ]] && brief="$override_brief"
  [[ -n "$brief" ]] || brief="$worker"
  if [[ -z "$ticket" ]]; then
    # Degrade gracefully: render without the [#<ticket>] segment rather than
    # hard-failing, but keep the one-worker-one-ticket policy violation visible.
    echo "workers.sh: warning: $worker has no ticket recorded -- retitling without [#<ticket>]" >&2
    ( cd "$AI_HARNESS_DIR" && \
      python3 -m remote_control titles set \
        --id "$worker" --cwd "$wdir" \
        --sub "MGR${ord}-W${wk}" \
        "$brief" )
    return 0
  fi
  ( cd "$AI_HARNESS_DIR" && \
    python3 -m remote_control titles set \
      --id "$worker" --cwd "$wdir" \
      --sub "MGR${ord}-W${wk}" \
      --sub "#${ticket}" \
      "$brief" )
}

# Idempotent one-shot: rewrite legacy `[MGR-<count>] Managing N workers` titles
# (the old format) to the new `[NICK.host][MGR-<ord>] <task> (N worker[s])`
# form for THIS manager. Other managers' titles are left alone -- run
# migrate-titles separately under each manager session if needed. Note: `note`
# suffix in host segments is handled by the title watcher's normalize_host_segment
# pass, not here.
cmd_migrate_titles() {
  local apply=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --apply) apply=true; shift ;;
      *) die "migrate-titles: unknown arg: $1" ;;
    esac
  done
  require_jq
  local mgr current_title
  mgr="$(resolve_manager_id)" || return $?
  current_title="$(cd "$AI_HARNESS_DIR" && python3 -m remote_control sessions --json 2>/dev/null \
    | jq -r --arg m "$mgr" '.[] | select(.id == $m) | .title // empty')"
  if [[ -z "$current_title" ]]; then
    echo "migrate-titles: manager $mgr not found in active sessions"
    return 0
  fi
  # Legacy: literal "[MGR-<n>] Managing <m> workers" or "[MGR-<n>] Managing <m> worker"
  if ! [[ "$current_title" =~ \[MGR-[0-9]+\][[:space:]]Managing[[:space:]][0-9]+[[:space:]]worker ]]; then
    echo "migrate-titles: manager title not in legacy form -- nothing to do"
    echo "  current: $current_title"
    return 0
  fi
  # Extract the existing task label (everything after the legacy `Managing N
  # workers` segment, if any extra suffix was appended). Default to "Managing"
  # when there isn't one.
  local task="Managing"
  if $apply; then
    cmd_retitle "$task"
  else
    echo "migrate-titles: would re-render to '${task} (<count> workers)' via retitle"
    echo "  current : $current_title"
    echo "  pass --apply to PUT the new title"
  fi
}

usage() {
  cat <<'EOF'
workers.sh — manager-side worker tracking (per-manager JSONL state log)

Subcommands:
  register <worker_id> <dir> --ticket N [--brief TEXT]
  update   <worker_id> [--status S] [--note TEXT]
  close    <worker_id> [--reason TEXT]
  forget   <worker_id> [--reason TEXT]
  list     [--all] [--json]    (default: active only, table form)
  get      <worker_id>         (latest folded record as JSON)
  path                         (print state file path)
  loop-state                   (print 'armed' [+ armed_at] or 'disarmed')
  loop-arm                     (set the loop-armed marker for this manager)
  loop-disarm                  (clear the loop-armed marker)
  mgr-id                       (allocate / read this manager's host-scoped ordinal)
  mgr-cwd                      (read the cwd snapshotted on first mgr-id call)
  retitle "<task>"             (titles set --sub MGR-<ord> "<task> (N worker[s])")
  retitle-worker <wid> [brief] (titles set --sub MGR<ord>-W<k> --sub #<ticket> ...)
  migrate-titles [--apply]     (rewrite legacy [MGR-N] manager title; dry-run by default)

Env:
  MANAGER_CSE_ID      override manager id (else from CLAUDE_CODE_SESSION_ACCESS_TOKEN JWT)
  MANAGER_STATE_DIR   override state dir (default ~/.ai-harness/manager)
  AI_HARNESS_DIR      override ai-harness checkout location (default ~/dev/ai-harness)
EOF
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    register)        cmd_register        "$@" ;;
    update)          cmd_update          "$@" ;;
    close)           cmd_close           "$@" ;;
    forget)          cmd_forget          "$@" ;;
    mgr-id)          cmd_mgr_id          "$@" ;;
    mgr-cwd)         cmd_mgr_cwd         "$@" ;;
    retitle)         cmd_retitle         "$@" ;;
    retitle-worker)  cmd_retitle_worker  "$@" ;;
    migrate-titles)  cmd_migrate_titles  "$@" ;;
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
