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
#   mgr-audit                    (check the ordinal ledger against the LIVE session list)
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
  # SUPERSEDED vs TERMINAL -- the distinction that keeps this idempotent.
  #
  # A record carrying `superseded_by` means another cse now holds THIS ordinal,
  # so handing the old session its number back would collide (#129/#136): skip
  # it, and it gets a fresh one.
  #
  # A record retired with NO successor is TERMINAL -- the session is archived
  # and its initiative ended. Return it. Skipping those was an ordinal LEAK
  # (#146): `retitle-worker` calls mgr-id FOR THE MANAGER to build the
  # MGR<n>-W<m> tag, so a LIVE worker of an ARCHIVED manager minted a new
  # ordinal on every retitle -- one dead cse burned four (7, 14, 16, 19) and
  # every reconcile pass simply fed it another. Returning the terminal record
  # makes the ordinal stable, so the reconcile converges.
  #
  # Precedence: an ACTIVE record wins; else the most recent terminal-retired
  # one; else empty (all superseded -> fresh allocation, the #136 intent).
  jq -s -c --arg m "$mgr" \
    '[ .[] | select(.cse_id == $m) ] as $all
     | ( [ $all[] | select(has("retired_at") | not) ][0]
         // [ $all[] | select(has("superseded_by") | not) ][-1]
         // empty )' "$file"
}

# Per-host ordinal BAND. The ledger is per-machine but titles are GLOBAL, so
# two hosts allocating `max(own ledger)+1` both start at 1 and collide -- which
# is not theoretical: the first cross-host allocation produced a live
# [DD.m5][MGR-3] and a live [DEV.mini][MGR-3] simultaneously (#135). Disjoint
# bands make the collision impossible with NO shared state and no coordination
# at allocation time, which is the only shape that works for two hosts that
# cannot read each other's ledger.
#
# Echoes "<lo> <hi>", or nothing when this host has no band (unknown host with
# no override -> legacy unbanded behaviour, so nothing breaks silently).
# MANAGER_ORDINAL_BAND="lo-hi" overrides, for tests and new hosts.
_ordinal_band() {
  local host band
  if [[ -n "${MANAGER_ORDINAL_BAND:-}" ]]; then
    printf '%s %s\n' "${MANAGER_ORDINAL_BAND%%-*}" "${MANAGER_ORDINAL_BAND##*-}"
    return 0
  fi
  # Match the host by GLOB, not by literal nick. `hostname -s` returns the
  # machine name ("macmini", "m5note"), NOT the short nick ("mini", "m5"), so a
  # literal case arm matched neither host and every call silently fell through
  # to unbanded allocation -- which minted an out-of-band ord 4 on mini the
  # first time this shipped. Mirrors discovery.NICKNAME_RULES, which already
  # globs *macmini* -> mini; this is the same normalization, kept in sync by
  # hand because workers.sh can't import it.
  host="${REMOTE_CONTROL_HOST:-$(hostname -s)}"
  case "$host" in
    m5|*m5*)         band="1 99"    ;;
    mini|*macmini*)  band="100 199" ;;
    *)
      # LOUD, not silent. An unrecognized host allocating unbanded is exactly
      # how a cross-host collision gets minted (#135), so say so rather than
      # quietly reverting to the legacy behaviour.
      echo "workers.sh: warning: host '$host' has no ordinal band; allocating" \
           "UNBANDED, which can collide with another host. Set" \
           "REMOTE_CONTROL_HOST or MANAGER_ORDINAL_BAND, or add an arm to" \
           "_ordinal_band." >&2
      return 0 ;;
  esac
  printf '%s\n' "$band"
}

# Portable exclusive lock around the ordinals file. macOS ships no flock(1),
# so use mkdir -- atomic on every POSIX filesystem: exactly one racer creates
# the dir, the rest spin. Without this, cmd_mgr_id was a textbook TOCTOU
# (read max, compute max+1, append) and two managers allocating at once both
# read the same max and both appended the SAME ordinal -- which is how the
# live ledger ended up with two sessions on ord 3 (#129).
_ordinals_lock() {
  local lock deadline
  lock="$(ordinals_path).lock"
  deadline=$(( $(date +%s) + 10 ))
  while ! mkdir "$lock" 2>/dev/null; do
    if [[ $(date +%s) -ge $deadline ]]; then
      # Break a lock orphaned by a killed process rather than wedging forever;
      # 10s is far longer than the read-append this guards.
      rmdir "$lock" 2>/dev/null || true
      mkdir "$lock" 2>/dev/null && break
      die "mgr-id: could not acquire $lock"
    fi
    sleep 0.1
  done
  _ORDINALS_LOCK="$lock"
  trap '_ordinals_unlock' EXIT INT TERM
}

_ordinals_unlock() {
  [[ -n "${_ORDINALS_LOCK:-}" ]] && rmdir "$_ORDINALS_LOCK" 2>/dev/null || true
  _ORDINALS_LOCK=""
}

# True if *cse* is in the ACTIVE session list. Active-list MEMBERSHIP is the
# real archived test -- `sessions --all` reports arch=None even for archived
# sessions, which reads as "not archived" and has already misled one reader.
# Fails OPEN (returns true) when the list can't be read, so a transient API
# failure can't block every allocation on the host.
_manager_id_is_active() {
  local out
  out="$(cd "$AI_HARNESS_DIR" && python3 -m remote_control sessions list --ids-only 2>/dev/null)" || return 0
  # FAIL OPEN unless we positively hold a valid active list. Enforcing on
  # anything else would block allocation whenever the API hiccups -- and would
  # break any caller pointing AI_HARNESS_DIR at a stub (the workers.sh tests do
  # exactly that). "Valid" = at least one line shaped like a session id; the
  # check only has authority when it can actually see the roster.
  grep -qE '^cse_[A-Za-z0-9]+$' <<<"$out" || return 0
  grep -qx "$1" <<<"$out"
}

cmd_mgr_id() {
  require_jq
  local mgr file existing rec ord prior
  mgr="$(resolve_manager_id)" || return $?
  # Garbage in, garbage claimed: mini's ledger carries records whose cse_id is
  # "python3 (shimmed) -" and a bare uuid, i.e. resolve_manager_id's output was
  # never validated before being written as an identity. Refuse rather than
  # mint another one.
  [[ "$mgr" == cse_* ]] || die "mgr-id: refusing to allocate for a non-cse manager id: '$mgr'"
  file="$(ordinals_path)" || return $?
  # Fast path: already allocated, no lock needed (append-only file, and a
  # record for THIS manager can never be rewritten by someone else).
  existing="$(_ordinal_record)" || return $?
  if [[ -n "$existing" ]]; then
    printf '%s\n' "$(echo "$existing" | jq -r '.ord')"
    return 0
  fi
  # IDENTITY GUARD. Allocating is the destructive step, so verify the resolved
  # manager id is a LIVE session before minting anything. A spawned session that
  # inherited its parent's CLAUDE_CODE_SESSION_ACCESS_TOKEN reports the PARENT's
  # cse_id (#147), and every allocation under that identity is attributed to a
  # session that isn't the caller -- which is how one archived manager burned
  # four ordinals (#146). Refuse rather than warn: an ordinal minted under the
  # wrong identity corrupts the ledger, and unlike a title it can't be observed
  # and corrected later. Fixing spawn_env stops NEW sessions inheriting; this
  # catches every session already running with a bad env.
  if ! _manager_id_is_active "$mgr"; then
    die "mgr-id: refusing to allocate for '$mgr' -- it is not an ACTIVE session.
  This usually means THIS session inherited its parent's identity
  (CLAUDE_CODE_SESSION_ACCESS_TOKEN) and is acting as its predecessor (#147).
  Confirm with: python3 -m remote_control sessions list --json | grep <your cse>
  Then re-run with MANAGER_CSE_ID=<your own cse_id> set explicitly."
  fi
  _ordinals_lock
  # Re-read INSIDE the lock: a racer may have allocated for this same manager
  # between the fast-path check and the lock (double-checked locking).
  existing="$(_ordinal_record)" || { _ordinals_unlock; return 1; }
  if [[ -n "$existing" ]]; then
    _ordinals_unlock
    printf '%s\n' "$(echo "$existing" | jq -r '.ord')"
    return 0
  fi
  # Allocate the next ordinal WITHIN this host's band, so the number can never
  # collide with one minted on another host (#135). Unbanded hosts keep the
  # legacy global max+1.
  local lo hi
  read -r lo hi <<<"$(_ordinal_band)"
  if [[ -n "$lo" ]]; then
    if [[ -s "$file" ]]; then
      prior="$(jq -s --argjson lo "$lo" --argjson hi "$hi" \
        '[ .[] | .ord | select(. >= $lo and . <= $hi) ] | (max // ($lo - 1))' "$file")"
    else
      prior=$((lo - 1))
    fi
    ord=$((prior + 1))
    if [[ "$ord" -gt "$hi" ]]; then
      _ordinals_unlock
      die "mgr-id: host band $lo-$hi is exhausted; widen the band in _ordinal_band"
    fi
  else
    if [[ -s "$file" ]]; then
      prior="$(jq -s '[ .[] | .ord ] | (max // 0)' "$file")"
    else
      prior=0
    fi
    ord=$((prior + 1))
  fi
  # Record the HOST: the ledger is per-machine ($HOME/.ai-harness) but titles
  # are global, so two hosts can independently mint the same [MGR-N]. Stamping
  # the host makes a cross-host duplicate auditable after the fact.
  rec="$(jq -nc \
    --arg m "$mgr" --argjson o "$ord" \
    --arg cwd "$PWD" --arg ts "$(now_utc)" \
    --arg host "${REMOTE_CONTROL_HOST:-$(hostname -s)}" \
    '{cse_id:$m, ord:$o, cwd:$cwd, host:$host, allocated_at:$ts}')"
  printf '%s\n' "$rec" >> "$file"
  _ordinals_unlock
  printf '%s\n' "$ord"
}

# Audit the ordinal ledger against the LIVE session list. Exists because this
# ledger drifted twice from hand-checking: the first repair reconciled against
# connection_status (connected/disconnected), which is NOT archived-status, so
# four ordinals stayed "active" while their holders were archived -- including
# one whose own title said it had HANDED OVER to a live successor. Reconcile
# against ACTIVE sessions, and sweep EVERY live claimant rather than the ones
# someone happened to mention. Exits non-zero if the ledger is inconsistent.
cmd_mgr_audit() {
  require_jq
  local file; file="$(ordinals_path)" || return $?
  local lo hi; read -r lo hi <<<"$(_ordinal_band)"
  AI_HARNESS_DIR="$AI_HARNESS_DIR" LEDGER="$file" BAND_LO="$lo" BAND_HI="$hi" python3 - <<'PYAUDIT'
import json, os, re, subprocess, sys
led = [json.loads(l) for l in open(os.environ["LEDGER"]) if l.strip()]
def run(a):
    """Live session list, or [] on ANY failure (missing dir, non-zero exit,
    unparseable output). Returning [] rather than raising keeps the empty-list
    guard below as the single place that decides what an unreadable list
    means -- a traceback here would just be a noisier false alarm."""
    try:
        p = subprocess.run(
            ["python3", "-m", "remote_control", "sessions", "list"] + a,
            capture_output=True, text=True, cwd=os.environ["AI_HARNESS_DIR"])
        return json.loads(p.stdout) if p.returncode == 0 else []
    except (OSError, ValueError):
        return []
# ACTIVE-LIST MEMBERSHIP IS THE REAL ARCHIVED TEST. `sessions --all` reports
# arch=None even for archived sessions, which reads as "not archived" and has
# already caused one reader to doubt a correct classification. Absence from the
# ACTIVE list is what means archived.
active = {r["id"]: r for r in run(["--json"])}
# A transient API failure returns an empty list, which would make EVERY active
# claim look archived and every live claimant look missing -- a false alarm
# that could talk someone into a destructive "repair". An empty live list is
# never a legitimate audit input (this host is running at least this session),
# so refuse to judge rather than report a ledger-wide fault.
if not active:
    print("mgr-audit: could not read the live session list (empty result) -- "
          "refusing to audit; retry rather than trusting this as a fault",
          file=sys.stderr)
    sys.exit(2)
problems = []

claims = [r for r in led if "retired_at" not in r]
for r in claims:
    if r["cse_id"] not in active:
        problems.append(f"ord {r['ord']}: holder {r['cse_id']} is ARCHIVED "
                        f"(retire it; name a live successor if one exists)")

# IDENTITY MISATTRIBUTION (#147): a manager state log that is being WRITTEN
# recently but is OWNED by a non-active cse means some LIVE session is acting
# as that dead identity -- it inherited its parent's session token, so it
# allocates ordinals, records workers, and retitles under the predecessor's id.
# Detect it from disk (log mtime + active-list membership) rather than waiting
# for someone to notice a duplicate ordinal.
import glob, time
LOG_DIR = os.path.dirname(os.environ["LEDGER"])
for f in glob.glob(os.path.join(LOG_DIR, "cse_*.jsonl")):
    owner = os.path.basename(f)[:-len(".jsonl")]
    if owner in active:
        continue
    age_days = (time.time() - os.path.getmtime(f)) / 86400.0
    if age_days < 3:
        problems.append(
            f"{owner}: manager log written {age_days:.1f}d ago but that cse is "
            f"NOT active -- a live session is acting as this dead identity "
            f"(inherited session token, #147)")

# A `superseded_by` must mean "that cse holds THIS ordinal" -- not merely "the
# work continued over there". Mis-attributing it (the 0729 reconcile pointed an
# ordinal at a session holding no record) silently defeats the terminal-record
# rule, because a superseded record is skipped and the archived session mints a
# fresh ordinal anyway (#146). Validate the pointer rather than trusting the
# author.
by_ord = {}
for r in led:
    by_ord.setdefault(r["ord"], set()).add(r["cse_id"])
for r in led:
    sb = r.get("superseded_by")
    if sb and sb not in by_ord.get(r["ord"], set()):
        problems.append(
            f"ord {r['ord']}: superseded_by={sb} holds no record for this "
            f"ordinal -- an initiative handover is not an ordinal handover; "
            f"drop it so the record is TERMINAL (#146)")

lo = os.environ.get("BAND_LO")
hi = os.environ.get("BAND_HI")
if lo and hi:
    lo, hi = int(lo), int(hi)
    for r in claims:
        if not (lo <= r["ord"] <= hi):
            problems.append(
                f"ord {r['ord']}: OUTSIDE this host's band {lo}-{hi} "
                f"({r['cse_id']}) -- migrate it, or the band is only a convention")

ords = [r["ord"] for r in claims]
for o in sorted({x for x in ords if ords.count(x) > 1}):
    who = ", ".join(r["cse_id"] for r in claims if r["ord"] == o)
    problems.append(f"ord {o}: DUPLICATE active claim -- {who}")

held = {r["cse_id"] for r in claims}
this_host = os.environ.get("REMOTE_CONTROL_HOST", "")
for cid, s in active.items():
    m = re.search(r"\[MGR-(\d+)\]", s.get("title") or "")
    if not m or cid in held:
        continue
    nick = re.match(r"\[([^\]]+)\]", s.get("title") or "")
    seg = nick.group(1).rsplit(".", 1)[-1] if nick and "." in nick.group(1) else ""
    if seg and this_host and seg != this_host:
        continue  # another host owns its own ledger
    problems.append(f"MGR-{m.group(1)}: live claimant {cid} holds NO record "
                    f"(backfill it)")

print(f"{len(claims)} active claims, {len(led) - len(claims)} retired; "
      f"{len(active)} live sessions")
if problems:
    print("\nINCONSISTENT:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("ledger is consistent with the live session list")
PYAUDIT
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

# Compose & invoke `titles set` for the manager's own title. Emits the chained
#   [NICK.host][MGR-<ord>] <task> (N worker[s])
# form. A manager session never carries a ticket number — the `[#<ticket>]`
# bracket belongs to WORKER titles only (see cmd_retitle_worker).
# The nickname is derived from the manager session's OWN identity
# (`titles set --id <mgr>`, same repo-resolution the titles-watcher uses) — NOT
# from a --cwd override, which would derive the nick from wherever retitle
# happened to run (e.g. [AH] from ~/dev/ai-harness) and get flipped back by the
# watcher on its next pass (title churn, #105).
cmd_retitle() {
  local task=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      *)
        if [[ -z "$task" ]]; then task="$1"; shift
        else die "retitle: unexpected extra arg: $1"; fi ;;
    esac
  done
  [[ -n "$task" ]] || die "retitle: need <task-description>"
  require_jq
  local mgr ord count plural
  mgr="$(resolve_manager_id)" || return $?
  # Auto-allocate the ordinal on first retitle if mgr-id was never called.
  ord="$(cmd_mgr_id)" || return $?
  count="$(cmd_list --json | jq 'length')"
  plural="s"; [[ "$count" == "1" ]] && plural=""
  # Mark the ordinal as allocator-issued so `titles set` doesn't warn about a
  # hand-asserted number (#129) -- this IS the sanctioned path.
  ( cd "$AI_HARNESS_DIR" && \
    MANAGER_ORDINAL_ALLOCATED=1 \
    python3 -m remote_control titles set \
      --id "$mgr" \
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
      MANAGER_ORDINAL_ALLOCATED=1 \
      python3 -m remote_control titles set \
        --id "$worker" --cwd "$wdir" \
        --sub "MGR${ord}-W${wk}" \
        "$brief" )
    return 0
  fi
  ( cd "$AI_HARNESS_DIR" && \
    MANAGER_ORDINAL_ALLOCATED=1 \
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
  mgr-audit                    (check the ordinal ledger against the LIVE session list)
  mgr-cwd                      (read the cwd snapshotted on first mgr-id call)
  retitle "<task>"             (titles set --id <mgr> --sub MGR-<ord> "...")
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
    mgr-audit)       cmd_mgr_audit       "$@" ;;
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
