#!/bin/bash
# registry.sh — the manager REGISTRY: who owns what, and how work routes.
#
# Sits alongside workers.sh (the per-manager worker log) and answers.sh (the
# boss's answer feed) in the same state dir, and follows the same conventions:
# append-only JSONL folded on read, a mkdir-based lock (macOS has no flock(1)),
# records are NEVER rewritten or recycled, and $MANAGER_STATE_DIR overrides the
# dir for tests.
#
# Two manager KINDS coexist (the hybrid model, #145):
#   - domain  : a STANDING manager owning a domain (e.g. skill-manager→"skills")
#   - project : an AD-HOC, time-boxed manager tied to a board/repo
#
# Responsibilities are drawn from a small CONTROLLED VOCABULARY and normalized
# on BOTH write and read (lowercase, trim, plural-tolerant, aliased) so
# "skill"/"Skills"/"skills " all resolve to the same token. An out-of-vocab
# token is ACCEPTED but WARNED about — unknown-but-visible is fine;
# unknown-and-silent is the failure mode we refuse (#145 gap 1).
#
# Domain responsibilities are EXCLUSIVE: `register --kind domain` refuses a
# responsibility already held by another active domain manager (pass --force to
# override). Project managers MAY overlap. `audit` flags a domain double-claim
# as a fault either way (#145 gap 2).
#
# Subcommands:
#   register --kind domain|project --name "<n>" --goals "<…>"
#            --responsibilities "skills,infra" [--board N] [--project "<repo>"]
#            [--cse <id>] [--mgr-ord N] [--force]
#   list     [--all] [--json]            (active only by default, table form)
#   lookup   --responsibility skills     (→ owning active manager's cse_id; exit 4 if none)
#   set-report --cse <id> --note "<…>" [--status reported|unreachable-busy|no-response]
#   retire   --cse <id> [--reason "<…>"]
#   audit    [--fix]                     (reconcile ledger vs LIVE sessions, both directions)
#   projects [--json]                    (boards + supplement, owner-annotated, flag unowned)
#   canon "<csv>"                        (debug: print normalized responsibilities as JSON)
#   path                                 (print the registry file path)
#   progress-path                        (print the progress-log path)
#
# Manager id resolution (for register without --cse) mirrors workers.sh:
#   1. $MANAGER_CSE_ID   2. JWT in $CLAUDE_CODE_SESSION_ACCESS_TOKEN.
#
# Env:
#   MANAGER_STATE_DIR      state dir (default ~/.ai-harness/manager)
#   AI_HARNESS_DIR         ai-harness checkout (default ~/dev/ai-harness)
#   REGISTRY_NOW           override the entry timestamp (tests; else `date -u`)
#   REGISTRY_LIVE_JSON     path to a JSON session list, for `audit` in tests
#                          (else `remote_control sessions list --json`)
#   SYSTEM_INITIATIVES_FILE  supplement path (default
#                            $AI_HARNESS_DIR/skills/meta-manage/system-initiatives.jsonl)
#   REGISTRY_BOARDS_JSON   path to a JSON list of boards, for `projects` in tests
#                          (else the built-in board list)

set -euo pipefail

STATE_DIR="${MANAGER_STATE_DIR:-$HOME/.ai-harness/manager}"
AI_HARNESS_DIR="${AI_HARNESS_DIR:-$HOME/dev/ai-harness}"

die() { echo "registry.sh: $*" >&2; exit 2; }
require_jq() { command -v jq >/dev/null 2>&1 || die "jq is required"; }
now_utc() { if [[ -n "${REGISTRY_NOW:-}" ]]; then printf '%s\n' "$REGISTRY_NOW"; else date -u +%Y-%m-%dT%H:%M:%SZ; fi; }

resolve_manager_id() {
  if [[ -n "${MANAGER_CSE_ID:-}" ]]; then printf '%s\n' "$MANAGER_CSE_ID"; return 0; fi
  AI_HARNESS_DIR="$AI_HARNESS_DIR" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["AI_HARNESS_DIR"])
try:
    from remote_control.session_list import own_session_id_from_env
except Exception as e:
    sys.stderr.write(f"registry.sh: cannot import ai-harness helper: {e}\n")
    sys.exit(2)
sid = own_session_id_from_env(dict(os.environ))
if not sid:
    sys.stderr.write("registry.sh: could not resolve own cse_id "
                     "(set MANAGER_CSE_ID or run inside a Claude Code session)\n")
    sys.exit(3)
print(sid)
PY
}

registry_path() {
  mkdir -p "$STATE_DIR" >/dev/null
  chmod 700 "$STATE_DIR" 2>/dev/null || true
  printf '%s/registry.jsonl\n' "$STATE_DIR"
}
progress_path() {
  mkdir -p "$STATE_DIR" >/dev/null
  printf '%s/progress.jsonl\n' "$STATE_DIR"
}
ordinals_path() { printf '%s/ordinals.jsonl\n' "$STATE_DIR"; }

# Read this manager's allocated ordinal from ordinals.jsonl (skip RETIRED
# records, exactly like workers.sh _ordinal_record). Empty if none.
_ordinal_for() {
  local mgr="$1" file
  file="$(ordinals_path)"
  [[ -s "$file" ]] || { printf ''; return 0; }
  jq -s -r --arg m "$mgr" \
    '[ .[] | select(.cse_id == $m and (has("retired_at") | not)) ][0] | .ord // empty' "$file"
}

# ---- responsibility normalization + controlled vocabulary (#145 gap 1) -------
# Normalize a comma-separated responsibilities string to canonical tokens.
# Prints {"canon":[...],"unknown":[...]}. Identical logic is used on register
# AND lookup so a value written one way always matches a query written another.
# The vocab is intentionally small and repo-owned; extend it here (one place),
# never by tolerating divergent free text.
_canon_resp() {
  RESP="${1:-}" python3 - <<'PY'
import json, os
raw = os.environ.get("RESP", "")
# alias -> canonical. Keep this the single source of truth for the vocabulary.
VOCAB = {
    "skill": "skills", "skills": "skills",
    "infra": "infra", "infrastructure": "infra", "ops": "infra",
    "trade": "trading", "trades": "trading", "trading": "trading",
    "finance": "finance", "financial": "finance", "fin": "finance", "finances": "finance",
    "legal": "legal", "estate": "legal", "legal/estate": "legal", "law": "legal",
    "deck": "deck", "decks": "deck", "slide": "deck", "slides": "deck", "presentation": "deck",
    "session": "sessions", "sessions": "sessions", "naming": "sessions", "titles": "sessions",
    "harness": "harness", "coordination": "coordination", "meta": "coordination",
}
canon, unknown, seen = [], [], set()
for tok in raw.split(","):
    t = tok.strip().lower()
    if not t:
        continue
    c = VOCAB.get(t)
    if c is None and t.endswith("s"):
        c = VOCAB.get(t[:-1])            # plural-tolerant fallback
    if c is None:
        c = t                            # accept unknown, but flag it
        unknown.append(t)
    if c not in seen:
        seen.add(c); canon.append(c)
print(json.dumps({"canon": canon, "unknown": unknown}))
PY
}

# ---- lock (mkdir is atomic on every POSIX fs; macOS has no flock(1)) ----------
# Guards the append so two managers registering at once can't interleave. Same
# pattern workers.sh/answers.sh use; the ordinals TOCTOU saga is why it's here.
_registry_lock() {
  local lock deadline
  lock="$(registry_path).lock"
  deadline=$(( $(date +%s) + 10 ))
  while ! mkdir "$lock" 2>/dev/null; do
    if [[ $(date +%s) -ge $deadline ]]; then
      rmdir "$lock" 2>/dev/null || true
      mkdir "$lock" 2>/dev/null && break
      die "could not acquire $lock"
    fi
    sleep 0.1
  done
  _REGISTRY_LOCK="$lock"
  trap '_registry_unlock' EXIT INT TERM
}
_registry_unlock() {
  [[ -n "${_REGISTRY_LOCK:-}" ]] && rmdir "$_REGISTRY_LOCK" 2>/dev/null || true
  _REGISTRY_LOCK=""
}

append_event() {
  local file; file="$(registry_path)"
  _registry_lock
  printf '%s\n' "$1" >> "$file"
  _registry_unlock
}

# Fold the append-only log into one record per manager (keyed by cse_id).
# register creates/updates the fields; report stamps last_report_at/last_note/
# last_report_status; retire flips status. registered_at is preserved from the
# first register.
_folded() {
  local file; file="$(registry_path)"
  [[ -s "$file" ]] || { echo "[]"; return 0; }
  jq -s '
    reduce .[] as $e ({};
      if $e.event == "register" then
        (.[$e.cse_id] // {}) as $cur
        | .[$e.cse_id] = ($cur + {
            cse_id: $e.cse_id, mgr_ord: $e.mgr_ord, kind: $e.kind,
            name: $e.name, goals: $e.goals,
            responsibilities: $e.responsibilities,
            board: $e.board, project: $e.project,
            status: "active",
            registered_at: ($cur.registered_at // $e.ts),
            last_ts: $e.ts })
      elif $e.event == "report" then
        (.[$e.cse_id] // {cse_id:$e.cse_id, status:"active"}) as $cur
        | .[$e.cse_id] = ($cur + {last_report_at:$e.ts, last_note:$e.note,
                                  last_report_status:($e.status // "reported"), last_ts:$e.ts})
      elif $e.event == "retire" then
        (.[$e.cse_id] // {cse_id:$e.cse_id}) as $cur
        | .[$e.cse_id] = ($cur + {status:"retired", retire_reason:$e.reason, last_ts:$e.ts})
      else . end)
    | [ .[] ] | sort_by(.mgr_ord // 0)
  ' "$file"
}

cmd_register() {
  local kind="" name="" goals="" resp="" board="" project="" cse="" ord="" force=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --kind)             kind="${2:-}";    shift 2 ;;
      --name)             name="${2:-}";    shift 2 ;;
      --goals)            goals="${2:-}";   shift 2 ;;
      --responsibilities) resp="${2:-}";    shift 2 ;;
      --board)            board="${2:-}";   shift 2 ;;
      --project)          project="${2:-}"; shift 2 ;;
      --cse)              cse="${2:-}";     shift 2 ;;
      --mgr-ord)          ord="${2:-}";     shift 2 ;;
      --force)            force=true;       shift ;;
      *) die "register: unknown arg: $1" ;;
    esac
  done
  require_jq
  [[ "$kind" == "domain" || "$kind" == "project" ]] || die "register: --kind must be domain|project (got: '${kind:-empty}')"
  [[ -n "$name" ]]  || die "register: --name is required"
  [[ -n "$goals" ]] || die "register: --goals is required"
  [[ -n "$resp" ]]  || die "register: --responsibilities \"a,b\" is required"
  if [[ "$kind" == "project" && -z "$board" && -z "$project" ]]; then
    die "register: a project manager needs --board N or --project \"<repo>\""
  fi
  [[ -n "$cse" ]] || cse="$(resolve_manager_id)" || return $?
  [[ "$cse" == cse_* ]] || die "register: refusing a non-cse manager id: '$cse'"
  if [[ -z "$ord" ]]; then
    ord="$(_ordinal_for "$cse")"
    [[ -n "$ord" ]] || die "register: no ordinal allocated for $cse yet (run 'workers.sh retitle' first, or pass --mgr-ord N)"
  fi

  # Normalize responsibilities; warn (do not fail) on out-of-vocab tokens.
  local cj resp_json unknown
  cj="$(_canon_resp "$resp")"
  resp_json="$(echo "$cj" | jq -c '.canon')"
  unknown="$(echo "$cj" | jq -r '.unknown | join(", ")')"
  [[ "$(echo "$resp_json" | jq 'length')" -gt 0 ]] || die "register: no usable responsibilities in '$resp'"
  if [[ -n "$unknown" ]]; then
    echo "registry.sh: warning: responsibilities not in the controlled vocabulary: $unknown" >&2
    echo "registry.sh:   (accepted, but an out-of-vocab token only matches a lookup spelled EXACTLY the same — add it to _canon_resp's VOCAB if it should be canonical)" >&2
  fi

  # Domain exclusivity (#145 gap 2): a domain responsibility may have only one
  # active domain owner. Refuse a conflict unless --force. A project manager may
  # overlap freely, so this check is domain-only.
  if [[ "$kind" == "domain" ]]; then
    local conflicts
    conflicts="$(_folded | jq -r --argjson rs "$resp_json" --arg me "$cse" '
      [ .[] | select(.status=="active" and .kind=="domain" and .cse_id!=$me
          and ((.responsibilities // []) as $own
               | ($rs | map(. as $r | $own | index($r)) | any))) ]
      | map("MGR-\(.mgr_ord) \(.cse_id) owns [\((.responsibilities//[])|join(","))]")
      | join("; ")')"
    if [[ -n "$conflicts" ]]; then
      if $force; then
        echo "registry.sh: warning: --force overriding domain conflict: $conflicts" >&2
      else
        die "register: domain responsibility conflict (exclusive): $conflicts. Re-register with --force to override, or use --kind project for an overlapping ad-hoc manager."
      fi
    fi
  fi

  local board_json="null" project_json="null"
  [[ -n "$board" ]]   && board_json="$(jq -nc --arg b "${board#\#}" '$b')"
  [[ -n "$project" ]] && project_json="$(jq -nc --arg p "$project" '$p')"
  local rec
  rec="$(jq -nc \
    --arg ev register --arg cse "$cse" --argjson ord "$ord" \
    --arg kind "$kind" --arg name "$name" --arg goals "$goals" \
    --argjson resp "$resp_json" --argjson board "$board_json" \
    --argjson project "$project_json" --arg ts "$(now_utc)" \
    '{event:$ev, cse_id:$cse, mgr_ord:$ord, kind:$kind, name:$name,
      goals:$goals, responsibilities:$resp, board:$board, project:$project, ts:$ts}')"
  append_event "$rec"
  printf 'registered %s (MGR-%s, kind=%s, name=%s, owns=%s)\n' \
    "$cse" "$ord" "$kind" "$name" "$(echo "$resp_json" | jq -r 'join(",")')"
}

cmd_set_report() {
  local cse="" note="" status="reported"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cse)    cse="${2:-}";    shift 2 ;;
      --note)   note="${2:-}";   shift 2 ;;
      --status) status="${2:-}"; shift 2 ;;
      *) die "set-report: unknown arg: $1" ;;
    esac
  done
  require_jq
  [[ -n "$cse" ]]  || die "set-report: --cse <id> is required"
  # A non-response is a legitimate outcome to record (#145 gap 3): the boss must
  # see "couldn't reach", never silence. So --note is only required when the
  # status is a real report.
  case "$status" in
    reported|unreachable-busy|no-response) ;;
    *) die "set-report: --status must be reported|unreachable-busy|no-response (got '$status')" ;;
  esac
  if [[ "$status" == "reported" && -z "$note" ]]; then
    die "set-report: --note \"<progress>\" is required for a 'reported' status"
  fi
  [[ -n "$note" ]] || note="(${status})"
  local ts; ts="$(now_utc)"
  local rec
  rec="$(jq -nc --arg ev report --arg cse "$cse" --arg note "$note" --arg st "$status" --arg ts "$ts" \
    '{event:$ev, cse_id:$cse, note:$note, status:$st, ts:$ts}')"
  append_event "$rec"
  # Also append to the consolidated progress log — the ONE place the boss reads
  # each daily sweep. Authority split: registry.last_note is the CURRENT-STATE
  # snapshot (latest only, folded); progress.jsonl is the append-only HISTORY.
  # They never disagree because this one call writes both from the same note.
  local prog; prog="$(progress_path)"
  local mgr_ord; mgr_ord="$(_folded | jq -r --arg c "$cse" '.[] | select(.cse_id==$c) | .mgr_ord // "?"')"
  jq -nc --arg cse "$cse" --arg ord "${mgr_ord:-?}" --arg note "$note" --arg st "$status" --arg ts "$ts" \
    '{cse_id:$cse, mgr_ord:$ord, note:$note, status:$st, ts:$ts}' >> "$prog"
  printf 'report recorded for %s (MGR-%s, status=%s) -> %s\n' "$cse" "${mgr_ord:-?}" "$status" "$prog"
}

cmd_retire() {
  local cse="" reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cse)    cse="${2:-}";    shift 2 ;;
      --reason) reason="${2:-}"; shift 2 ;;
      *) die "retire: unknown arg: $1" ;;
    esac
  done
  require_jq
  [[ -n "$cse" ]] || die "retire: --cse <id> is required"
  local rec
  rec="$(jq -nc --arg ev retire --arg cse "$cse" --arg r "$reason" --arg ts "$(now_utc)" \
    '{event:$ev, cse_id:$cse, reason:$r, ts:$ts}')"
  append_event "$rec"
  printf 'retired %s\n' "$cse"
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
  local folded; folded="$(_folded)"
  if ! $include_all; then
    folded="$(echo "$folded" | jq '[ .[] | select(.status == "active") ]')"
  fi
  if $as_json; then echo "$folded"; return 0; fi
  local count; count="$(echo "$folded" | jq 'length')"
  if [[ "$count" -eq 0 ]]; then
    if $include_all; then echo "(no managers registered)"
    else echo "(no active managers — try --all)"; fi
    return 0
  fi
  echo "$folded" | jq -r '.[] |
    "  MGR-\(.mgr_ord // "?")  [\(.status // "?")]  \(.kind // "?")  \(.name // "-")   \(.cse_id)\n      goals:  \(.goals // "")\n      owns:   \((.responsibilities // []) | join(", "))\n      board:  \(.board // "-")   project: \(.project // "-")\n      report: \(.last_report_at // "never") [\(.last_report_status // "-")]  \(.last_note // "")"'
}

# Return the cse_id of the ACTIVE manager owning a responsibility, preferring
# the (exclusive) DOMAIN owner over any overlapping project managers. Liveness
# is the caller's job (the routing rule checks sessions --json before relaying).
# Exit 4 when nobody owns it -> the routing rule's ESCALATE branch.
cmd_lookup() {
  local resp=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --responsibility) resp="${2:-}"; shift 2 ;;
      *) die "lookup: unknown arg: $1" ;;
    esac
  done
  require_jq
  [[ -n "$resp" ]] || die "lookup: --responsibility <x> is required"
  local q; q="$(_canon_resp "$resp" | jq -r '.canon[0] // empty')"
  [[ -n "$q" ]] || die "lookup: empty responsibility after normalization"
  local hits
  hits="$(_folded | jq -c --arg r "$q" \
    '[ .[] | select(.status=="active" and ((.responsibilities // []) | index($r))) ]')"
  local n; n="$(echo "$hits" | jq 'length')"
  if [[ "$n" -eq 0 ]]; then
    echo "registry.sh: no active manager owns responsibility '$q'" >&2
    exit 4
  fi
  local domain_hits; domain_hits="$(echo "$hits" | jq -c '[ .[] | select(.kind=="domain") ]')"
  local dn; dn="$(echo "$domain_hits" | jq 'length')"
  if [[ "$dn" -ge 1 ]]; then
    [[ "$dn" -gt 1 ]] && echo "registry.sh: warning: $dn active DOMAIN managers own '$q' (should be exclusive — run 'audit'); returning lowest ordinal" >&2
    echo "$domain_hits" | jq -r 'sort_by(.mgr_ord // 0) | .[0].cse_id'
  else
    [[ "$n" -gt 1 ]] && echo "registry.sh: note: $n active project managers own '$q' (no domain owner); returning lowest ordinal" >&2
    echo "$hits" | jq -r 'sort_by(.mgr_ord // 0) | .[0].cse_id'
  fi
}

# Live session list: from REGISTRY_LIVE_JSON (tests) or remote_control. Returns
# [] on any failure -- the empty-list guard in the caller decides what that means.
_live_sessions() {
  if [[ -n "${REGISTRY_LIVE_JSON:-}" ]]; then
    cat "$REGISTRY_LIVE_JSON"
    return 0
  fi
  ( cd "$AI_HARNESS_DIR" && python3 -m remote_control sessions list --json 2>/dev/null ) || echo "[]"
}

# Reconcile the ledger against the LIVE session list, BOTH directions. Mirrors
# workers.sh mgr-audit (#129): registry→live catches archived holders,
# live→registry catches unregistered live managers, and a domain double-claim is
# a fault regardless of liveness (#145 gap 2). Refuses an empty live list.
# Exit 1 if inconsistent.
cmd_audit() {
  local fix=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --fix) fix=true; shift ;;
      *) die "audit: unknown arg: $1" ;;
    esac
  done
  require_jq
  local folded live rc
  folded="$(_folded)"
  live="$(_live_sessions)"
  set +e
  REG="$folded" LIVE="$live" THIS_HOST="${REMOTE_CONTROL_HOST:-}" python3 - <<'PYAUDIT'
import json, os, re, sys
from collections import defaultdict
reg = json.loads(os.environ["REG"])
try:
    live = json.loads(os.environ["LIVE"])
except ValueError:
    live = []
if not live:
    print("registry-audit: could not read the live session list (empty) -- "
          "refusing to audit; retry rather than trusting this as a fault",
          file=sys.stderr)
    sys.exit(2)
live_ids = {r.get("id") for r in live}
active = [r for r in reg if r.get("status") == "active"]
problems = []

# registry -> live: an active entry whose session is archived is stale.
for r in active:
    if r["cse_id"] not in live_ids:
        problems.append(f"MGR-{r.get('mgr_ord','?')} ({r['cse_id']}): holder is "
                        f"ARCHIVED -- retire it")

# live -> registry: a live [MGR-N] session with no active record is unregistered.
# The registry is PER-HOST (this ~/.ai-harness), but the live picker shows
# cross-host sessions too, so skip a [MGR-N] whose nick host segment names a
# DIFFERENT host -- that manager lives in its own host's registry (mirrors
# workers.sh mgr-audit's cross-host guard).
this_host = os.environ.get("THIS_HOST", "")
held = {r["cse_id"] for r in active}
for s in live:
    cid = s.get("id")
    if cid in held:
        continue
    title = s.get("title") or ""
    # Match ONLY the manager bracket [MGR-N] (hyphen + closing bracket), NOT the
    # worker bracket [MGRn-Wk] -- a loose \[MGR-?\d+ also matched "[MGR13-W1]"
    # and false-flagged every worker as an unregistered manager.
    m = re.search(r"\[MGR-(\d+)\]", title)
    if not m:
        continue
    nick = re.match(r"\[([^\]]+)\]", title)
    seg = nick.group(1).rsplit(".", 1)[-1] if nick and "." in nick.group(1) else ""
    if seg and this_host and seg != this_host:
        continue  # another host owns its own registry
    problems.append(f"MGR-{m.group(1)} ({cid}): live manager holds NO active "
                    f"registry record -- register it")

# domain exclusivity: a responsibility owned by >1 active DOMAIN manager is a fault.
by_resp = defaultdict(set)
for r in active:
    if r.get("kind") == "domain":
        for x in (r.get("responsibilities") or []):
            by_resp[x].add(r["cse_id"])
for resp, owners in sorted(by_resp.items()):
    if len(owners) > 1:
        problems.append(f"responsibility '{resp}': DOUBLE-CLAIMED by domain "
                        f"managers {', '.join(sorted(owners))} -- must be exclusive")

print(f"{len(active)} active registrations; {len(live)} live sessions")
if problems:
    print("\nINCONSISTENT:")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("registry is consistent with the live session list")
PYAUDIT
  rc=$?
  set -e
  # --fix: retire the archived holders the auditor flagged (the only
  # auto-fixable direction; an unregistered live manager must register itself,
  # and a double-claim needs a human call on which owner keeps the domain).
  if $fix && [[ $rc -eq 1 ]]; then
    echo "$folded" | jq -r --argjson ids "$(echo "$live" | jq '[.[].id]')" \
      '.[] | select(.status=="active" and ((.cse_id | IN($ids[])) | not)) | .cse_id' \
      | while read -r stale; do
          [[ -n "$stale" ]] && cmd_retire --cse "$stale" --reason "audit --fix: session archived"
        done
    return 0
  fi
  return $rc
}

# Built-in canonical boards (the 6 GitHub Project boards). Overridable via
# REGISTRY_BOARDS_JSON for tests. Each: {board, name}.
_boards_json() {
  if [[ -n "${REGISTRY_BOARDS_JSON:-}" ]]; then cat "$REGISTRY_BOARDS_JSON"; return 0; fi
  cat <<'JSON'
[
  {"board": 2, "name": "Remote Control (ai-harness)"},
  {"board": 3, "name": "Divorcio / deck"},
  {"board": 4, "name": "Finex"},
  {"board": 5, "name": "dstrader"},
  {"board": 6, "name": "Trust funding"},
  {"board": 7, "name": "cos-console"}
]
JSON
}

_supplement_path() {
  printf '%s\n' "${SYSTEM_INITIATIVES_FILE:-$AI_HARNESS_DIR/skills/meta-manage/system-initiatives.jsonl}"
}

# Join the canonical boards + the system-initiative supplement, annotate each
# with its owning ACTIVE manager (board → by board number; initiative → by
# responsibility intersection), and flag any with NO active owner as a spin
# candidate.
cmd_projects() {
  local as_json=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) as_json=true; shift ;;
      *) die "projects: unknown arg: $1" ;;
    esac
  done
  require_jq
  local folded boards supp
  folded="$(_folded | jq -c '[ .[] | select(.status=="active") ]')"
  boards="$(_boards_json)"
  local supp_file; supp_file="$(_supplement_path)"
  if [[ -s "$supp_file" ]]; then supp="$(jq -s -c '.' "$supp_file")"; else supp="[]"; fi

  local out
  out="$(jq -n --argjson reg "$folded" --argjson boards "$boards" --argjson supp "$supp" '
    ($boards | map(. as $bd
      | ([ $reg[] | select((.board|tostring) == ($bd.board|tostring)) ] | sort_by(.mgr_ord//0)) as $own
      | {type:"board", board:$bd.board, name:$bd.name,
         owner:($own[0].cse_id // null), owner_ord:($own[0].mgr_ord // null)})) as $bl
    |
    ($supp | map(. as $it
      | ([ $reg[] | select( any((.responsibilities // [])[]; . as $r | ($it.responsibilities // []) | index($r)) ) ] | sort_by(.mgr_ord//0)) as $own
      | {type:"initiative", name:$it.name, description:$it.description,
         responsibilities:$it.responsibilities,
         owner:($own[0].cse_id // null), owner_ord:($own[0].mgr_ord // null)})) as $il
    | ($bl + $il) | map(. + {spin_candidate: (.owner == null)})
  ')"

  if $as_json; then echo "$out"; return 0; fi
  echo "$out" | jq -r '.[] |
    (if .spin_candidate then "  ⚠ SPIN " else "  ✓ owned" end) as $flag
    | "\($flag)  [\(.type)]  \(.name)\(if .board then " (board \(.board))" else "" end)   owner: \(if .owner then "MGR-\(.owner_ord) \(.owner)" else "— none —" end)"'
}

usage() {
  cat <<'EOF'
registry.sh — the manager registry (who owns what, how work routes)

Subcommands:
  register --kind domain|project --name "<n>" --goals "<…>"
           --responsibilities "skills,infra" [--board N] [--project "<repo>"]
           [--cse <id>] [--mgr-ord N] [--force]
  list     [--all] [--json]
  lookup   --responsibility <x>         (→ owning active manager's cse_id; exit 4 if none)
  set-report --cse <id> --note "<…>" [--status reported|unreachable-busy|no-response]
  retire   --cse <id> [--reason "<…>"]
  audit    [--fix]                      (reconcile vs LIVE sessions, both directions)
  projects [--json]                     (boards + supplement, owner-annotated)
  canon    "<csv>"                      (debug: normalized responsibilities as JSON)
  path                                  (print the registry file path)
  progress-path                         (print the progress-log path)

Env: MANAGER_STATE_DIR, AI_HARNESS_DIR, REGISTRY_NOW, REGISTRY_LIVE_JSON,
     SYSTEM_INITIATIVES_FILE, REGISTRY_BOARDS_JSON
EOF
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    register)      cmd_register   "$@" ;;
    list)          cmd_list       "$@" ;;
    lookup)        cmd_lookup     "$@" ;;
    set-report)    cmd_set_report "$@" ;;
    retire)        cmd_retire     "$@" ;;
    audit)         cmd_audit      "$@" ;;
    projects)      cmd_projects   "$@" ;;
    canon)         require_jq; _canon_resp "${1:-}" ;;
    path)          registry_path ;;
    progress-path) progress_path ;;
    -h|--help|help|"") usage ;;
    *) die "unknown subcommand: $sub" ;;
  esac
}

main "$@"
