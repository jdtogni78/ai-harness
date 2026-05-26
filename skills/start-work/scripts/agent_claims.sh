#!/usr/bin/env bash
# agent_claims.sh — multi-agent ticket coordination on the GitHub Project board.
#
# Lets one agent CLAIM a ticket (post a structured claim comment + assign +
# move to In Progress), RELEASE it, and lets ANY agent CHECK whether the
# claiming agent is still alive — so a stale "In Progress" ticket left behind
# by a disconnected agent can be safely reclaimed.
#
# Liveness is PASSIVE: an agent is "alive" if its session transcript (under
# ~/.claude/projects/<slug>/) was appended within AGENT_CLAIM_TTL_SECS (default
# 3600 = 1h) and didn't end on an API error / usage limit. Claims anchor on the
# WORKTREE PATH + HOST — the reliable join key, because the readable cse_ id
# lives only in the worktree path while the transcript filename is an unrelated
# UUID. The slug is the worktree path with every non-alphanumeric char -> "-"
# (the same encoding Claude Code uses; see session_port/claude.py).
#
# Cross-host: a claim made on another machine can't be liveness-verified here
# (its transcript is on that machine) -> verdict OFFHOST, check there.
#
# Usage:
#   agent_claims.sh whoami
#   agent_claims.sh check    <issue#>  [--repo OWNER/REPO]
#   agent_claims.sh todo                [--project N | --all-projects] [--owner O]
#   agent_claims.sh list                [--project N | --all-projects] [--owner O]
#   agent_claims.sh claim    <issue#>  [--repo OWNER/REPO] [--project N] [--owner O]
#   agent_claims.sh release  <issue#>  [--repo OWNER/REPO] [-- reason words...]
#   agent_claims.sh set-status <issue#> "<Status>" [--project N] [--owner O]
#
# Read-only:  whoami, check, todo, list.
#   todo — available (Status=Todo) tickets across boards, labeled by project +
#          repo (sweeps all boards by default; handles draft notes too). The
#          "what's free to pick up" view.
#   list — In-Progress tickets + their claim liveness (the "who's working what"
#          view).
# Mutating (need gh; status edits need the 'project' token scope): claim,
# release, set-status. claim/release degrade gracefully — the comment + assignee
# always post; if the board status edit can't run it prints the manual fallback.
#
# Env: AGENT_CLAIM_TTL_SECS (default 3600), AGENT_CLAIM_USER (default youruser),
#      AGENT_CLAIM_PROJECTS (default "1 2 3" — boards swept by --all-projects),
#      REMOTE_CONTROL_HOST (host nickname override).
set -euo pipefail

USER_TOKEN="${AGENT_CLAIM_USER:-youruser}"
TTL="${AGENT_CLAIM_TTL_SECS:-3600}"
DEFAULT_PROJECT="${AGENT_CLAIM_PROJECT:-2}"
DEFAULT_PROJECTS="${AGENT_CLAIM_PROJECTS:-1 2 3}"   # boards swept by --all-projects
DEFAULT_OWNER="${AGENT_CLAIM_OWNER:-youruser}"

# --- tool resolution (gh/jq are often not on launchd/sandbox PATH) -----------
find_bin() {
  local b; b="$(command -v "$1" 2>/dev/null || true)"
  [ -n "$b" ] && { printf '%s' "$b"; return; }
  local c; for c in "/opt/homebrew/bin/$1" "/usr/local/bin/$1"; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
}
GH="$(find_bin gh)"
JQ="$(find_bin jq)"
need_gh() { [ -n "$GH" ] || { echo "error: gh not found (install gh / GitHub CLI)" >&2; exit 3; }; }
need_jq() { [ -n "$JQ" ] || { echo "error: jq not found" >&2; exit 3; }; }

# --- identity ----------------------------------------------------------------
agent_host() {
  if [ -n "${REMOTE_CONTROL_HOST:-}" ]; then
    printf '%s' "$REMOTE_CONTROL_HOST" | tr 'A-Z' 'a-z'; return
  fi
  local h; h="$(hostname 2>/dev/null | tr 'A-Z' 'a-z')"
  case "$h" in
    *macbook*) echo note;;
    *macmini*) echo mini;;
    *) echo "${h%%.*}";;
  esac
}

agent_worktree() { git rev-parse --show-toplevel 2>/dev/null || echo ""; }
agent_branch()   { git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?"; }

# slug for a worktree path = every non-alphanumeric -> "-" (Claude Code encoding)
slug_of() { printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'; }

# newest transcript jsonl for a worktree path (empty if none)
transcript_for() {
  local dir="$HOME/.claude/projects/$(slug_of "$1")"
  [ -d "$dir" ] || return 0
  ls -t "$dir"/*.jsonl 2>/dev/null | head -1
}

# epoch mtime of a file (BSD stat first, GNU fallback)
mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# did the transcript end on an API error / usage limit? (true/false)
ended_on_error() {
  [ -n "$JQ" ] || { echo false; return; }
  "$JQ" -rs '(map(select(.timestamp))|last) as $l
    | (($l.message.content[]?.text // "")|test("API Error|usage limit|ECONNRESET";"i"))' \
    "$1" 2>/dev/null || echo false
}

# identity token: youruser+<worktree-basename>@<host>
agent_token() {
  local wt host base
  wt="$(agent_worktree)"; host="$(agent_host)"
  base="${wt##*/}"; [ -z "$base" ] && base="(no-worktree)"
  printf '%s+%s@%s' "$USER_TOKEN" "$base" "$host"
}

cmd_whoami() {
  local wt host br tx mt now age err
  wt="$(agent_worktree)"; host="$(agent_host)"; br="$(agent_branch)"
  tx="$(transcript_for "$wt")"
  echo "token    : $(agent_token)"
  echo "worktree : ${wt:-(not in a git worktree)}"
  echo "branch   : $br"
  echo "host     : $host"
  if [ -n "$tx" ]; then
    mt="$(mtime_of "$tx")"; now="$(date +%s)"; age=$(( now - mt )); err="$(ended_on_error "$tx")"
    echo "transcript: $tx"
    echo "  last-append: $((age/60))m ago   ended-on-error: $err   (TTL ${TTL}s)"
  else
    echo "transcript: (none found for this worktree slug)"
  fi
}

# --- verdict for a worktree+host claim ---------------------------------------
# echoes one of: ALIVE | STALE | DEAD | OFFHOST | NOCLAIM, plus a detail line.
liveness_verdict() {
  local wt="$1" claim_host="$2" this_host tx mt now age err
  this_host="$(agent_host)"
  if [ -n "$claim_host" ] && [ "$claim_host" != "$this_host" ]; then
    echo "OFFHOST claimed on '$claim_host' (this host is '$this_host') — verify there"
    return
  fi
  tx="$(transcript_for "$wt")"
  if [ -z "$tx" ]; then
    echo "DEAD no transcript for worktree slug (worktree gone / never logged here)"
    return
  fi
  err="$(ended_on_error "$tx")"
  if [ "$err" = "true" ]; then
    echo "DEAD session ended on API error / usage limit ($tx)"
    return
  fi
  mt="$(mtime_of "$tx")"; now="$(date +%s)"; age=$(( now - mt ))
  if [ "$age" -ge "$TTL" ]; then
    echo "STALE no transcript activity for $((age/60))m (TTL $((TTL/60))m) — reclaimable"
  else
    echo "ALIVE active $((age/60))m ago — do NOT take this ticket"
  fi
}

# --- repo / comment helpers --------------------------------------------------
resolve_repo() {
  if [ -n "${1:-}" ]; then printf '%s' "$1"; return; fi
  "$GH" repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo ""
}

# print the raw body of the most recent AGENT-CLAIM / AGENT-RELEASE comment
latest_claim_comment() {
  local issue="$1" repo="$2"
  "$GH" issue view "$issue" --repo "$repo" --json comments \
    --jq '.comments | map(select(.body|test("^(🤖 )?AGENT-(CLAIM|RELEASE)";"m"))) | last | .body // ""' \
    2>/dev/null
}

# --- commands ----------------------------------------------------------------
cmd_check() {
  need_gh; need_jq
  local issue="$1" repo body kind wt host
  repo="$(resolve_repo "${REPO:-}")"
  [ -n "$repo" ] || { echo "error: could not resolve repo (pass --repo OWNER/REPO)" >&2; exit 2; }
  body="$(latest_claim_comment "$issue" "$repo")"
  if [ -z "$body" ]; then
    echo "NOCLAIM #$issue ($repo): no agent claim comment — free to claim"
    return
  fi
  kind="$(printf '%s\n' "$body" | grep -oE 'AGENT-(CLAIM|RELEASE)' | head -1)"
  wt="$(printf '%s\n' "$body" | sed -n 's/^worktree: *//p' | head -1)"
  host="$(printf '%s\n' "$body" | sed -n 's/^host: *//p' | head -1)"
  if [ "$kind" = "AGENT-RELEASE" ]; then
    echo "RELEASED #$issue ($repo): last claim was released by ${host:-?} — free to claim"
    return
  fi
  printf '#%s (%s) claimed on %s — ' "$issue" "$repo" "${host:-?}"
  liveness_verdict "$wt" "$host"
}

cmd_claim() {
  need_gh
  local issue="$1" repo wt br host token body tmp
  repo="$(resolve_repo "${REPO:-}")"
  [ -n "$repo" ] || { echo "error: could not resolve repo (pass --repo OWNER/REPO)" >&2; exit 2; }
  wt="$(agent_worktree)"; br="$(agent_branch)"; host="$(agent_host)"; token="$(agent_token)"
  tmp="$(mktemp -t agentclaim)"
  cat >"$tmp" <<EOF
🤖 AGENT-CLAIM \`$token\`
\`\`\`
worktree: $wt
branch: $br
host: $host
claimed: $(date -u +%Y-%m-%dT%H:%M:%SZ)
\`\`\`
_Passive liveness, ${TTL}s (1h) TTL — reclaimable if this session goes stale. See \`start-work\` / \`resume-work\`._
EOF
  "$GH" issue comment "$issue" --repo "$repo" --body-file "$tmp"
  rm -f "$tmp"
  "$GH" issue edit "$issue" --repo "$repo" --add-assignee "$DEFAULT_OWNER" >/dev/null 2>&1 \
    && echo "assigned #$issue -> $DEFAULT_OWNER" \
    || echo "note: could not add assignee (non-fatal)"
  cmd_set_status "$issue" "In Progress" "$repo" || true
  echo "claimed #$issue ($repo) as $token"
}

cmd_release() {
  need_gh
  local issue="$1"; shift || true
  local repo token reason tmp
  repo="$(resolve_repo "${REPO:-}")"
  [ -n "$repo" ] || { echo "error: could not resolve repo (pass --repo OWNER/REPO)" >&2; exit 2; }
  token="$(agent_token)"; reason="${*:-released}"
  tmp="$(mktemp -t agentrel)"
  cat >"$tmp" <<EOF
🤖 AGENT-RELEASE \`$token\`
\`\`\`
host: $(agent_host)
released: $(date -u +%Y-%m-%dT%H:%M:%SZ)
reason: $reason
\`\`\`
EOF
  "$GH" issue comment "$issue" --repo "$repo" --body-file "$tmp"
  rm -f "$tmp"
  echo "released #$issue ($repo): $reason"
}

# set-status <issue#> "<Status>" [repo] — resolve the board's Status field + option ids
# and move the issue's project item. Best-effort: needs the 'project' scope.
# Pass repo (OWNER/REPO) to disambiguate when two repos share the same issue#
# on the same board (e.g. AppOne-archive#9 vs app-two-aws#9); falls back
# to global $REPO so the `set-status` CLI subcommand's --repo flag is honored.
cmd_set_status() {
  need_gh; need_jq
  local issue="$1" status="$2" repo="${3:-${REPO:-}}"
  local proj="${PROJECT:-$DEFAULT_PROJECT}" owner="${OWNER:-$DEFAULT_OWNER}"
  local proj_id field_id opt_id item_id
  proj_id="$("$GH" project view "$proj" --owner "$owner" --format json 2>/dev/null | "$JQ" -r '.id // empty')"
  if [ -z "$proj_id" ]; then
    echo "note: can't reach project #$proj (owner $owner) — set Status='$status' in the board UI." \
         "Need 'project' scope? gh auth refresh -s project --hostname github.com" >&2
    return 1
  fi
  field_id="$("$GH" project field-list "$proj" --owner "$owner" --format json 2>/dev/null \
    | "$JQ" -r '.fields[] | select(.name=="Status") | .id // empty')"
  opt_id="$("$GH" project field-list "$proj" --owner "$owner" --format json 2>/dev/null \
    | "$JQ" -r --arg s "$status" '.fields[] | select(.name=="Status") | .options[]? | select(.name==$s) | .id // empty')"
  # Filter by (number, repository) when repo is known — number alone collides
  # across repos sharing a board. .content.repository is "OWNER/REPO" in gh's
  # project item-list output (same field cmd_list / cmd_todo print).
  if [ -n "$repo" ]; then
    item_id="$("$GH" project item-list "$proj" --owner "$owner" --limit 1000 --format json 2>/dev/null \
      | "$JQ" -r --argjson n "$issue" --arg r "$repo" \
          '.items[] | select(.content.number==$n and .content.repository==$r) | .id // empty' | head -1)"
  else
    item_id="$("$GH" project item-list "$proj" --owner "$owner" --limit 1000 --format json 2>/dev/null \
      | "$JQ" -r --argjson n "$issue" '.items[] | select(.content.number==$n) | .id // empty' | head -1)"
  fi
  if [ -z "$field_id" ] || [ -z "$opt_id" ] || [ -z "$item_id" ]; then
    echo "note: couldn't resolve item/field/option for #$issue ('$status') — set it in the board UI." >&2
    return 1
  fi
  "$GH" project item-edit --id "$item_id" --project-id "$proj_id" \
    --field-id "$field_id" --single-select-option-id "$opt_id" >/dev/null \
    && echo "status #$issue -> $status" \
    || { echo "note: item-edit failed — set Status='$status' in the board UI." >&2; return 1; }
}

# board title for a project number (empty if unreachable)
project_title() {
  "$GH" project view "$1" --owner "${OWNER:-$DEFAULT_OWNER}" --format json 2>/dev/null \
    | "$JQ" -r '.title // empty'
}

# which boards to act on: --all-projects → all; --project N → just N; else default
projects_to_use() {
  if [ -n "$ALLPROJ" ]; then printf '%s' "$DEFAULT_PROJECTS"
  elif [ -n "$PROJECT" ]; then printf '%s' "$PROJECT"
  else printf '%s' "$1"; fi   # caller's fallback ($DEFAULT_PROJECT or $DEFAULT_PROJECTS)
}

# todo — available (Status=Todo) tickets across boards, labeled by project + repo.
# Defaults to sweeping ALL boards (the cross-board "what can I pick up" view);
# narrow with --project N. Shows draft notes (no issue #/repo) too.
#
# Labels: `gh project item-list` does NOT return issue labels, so we fetch them
# separately — one `gh issue list --json number,labels` per distinct repo. macOS
# ships bash 3.2 (no associative arrays), so the cache is a newline-delimited
# string of "repo#num<TAB>labels" rows looked up with pure parameter expansion.
cmd_todo() {
  need_gh; need_jq
  local owner="${OWNER:-$DEFAULT_OWNER}" projs p title n lbl rest
  local fetched=" " label_map=""
  projs="$(projects_to_use "$DEFAULT_PROJECTS")"
  echo "Available (Todo) tickets ($owner):"
  echo
  for p in $projs; do
    title="$(project_title "$p")"; [ -n "$title" ] || title="project"
    printf '▸ %s (#%s)\n' "$title" "$p"
    n=0
    # jq emits "-" placeholders for missing number/repo: with IFS=tab, empty
    # fields collapse (tab is IFS-whitespace), so never let a field be empty.
    while IFS=$'\t' read -r num repo title2; do
      [ -n "$title2" ] || continue
      n=$((n+1))
      if [ "$num" != "-" ]; then
        # Fetch this repo's open-issue labels once; cache as "repo#num<TAB>labels".
        case "$fetched" in
          *" $repo "*) : ;;
          *)
            fetched+="$repo "
            label_map+="$("$GH" issue list --repo "$repo" --state open --limit 300 \
                            --json number,labels 2>/dev/null \
                          | "$JQ" -r --arg r "$repo" '.[]
                              | [($r + "#" + (.number|tostring)),
                                 ([.labels[].name] | join(","))] | @tsv')"$'\n'
            ;;
        esac
        # Pull this issue's labels out of the cache: strip up to "repo#num<TAB>",
        # then keep through the next newline. The trailing TAB anchors the key so
        # "#1" never matches "#16".
        lbl=""
        case "$label_map" in
          *"$repo#$num"$'\t'*)
            rest="${label_map#*$repo#$num$'\t'}"
            lbl="${rest%%$'\n'*}"
            ;;
        esac
        if [ -n "$lbl" ]; then
          printf '   #%-4s %-24s %s  [%s]\n' "$num" "${repo##*/}" "$title2" "$lbl"
        else
          printf '   #%-4s %-24s %s\n' "$num" "${repo##*/}" "$title2"
        fi
      else
        printf '   %-5s %-24s %s\n' "—" "(draft)" "$title2"
      fi
    done < <("$GH" project item-list "$p" --owner "$owner" --limit 1000 --format json 2>/dev/null \
        | "$JQ" -r '.items[] | select(.status=="Todo")
                    | [(.content.number // "-"), (.content.repository // "-"),
                       (.content.title // .title // "?")] | @tsv')
    [ "$n" -eq 0 ] && echo "   (none)"
    echo
  done
  echo "Claim one with: agent_claims.sh check <#> && agent_claims.sh claim <#> --project <N>"
}

cmd_list() {
  need_gh; need_jq
  local owner="${OWNER:-$DEFAULT_OWNER}" projs p
  projs="$(projects_to_use "$DEFAULT_PROJECT")"
  for p in $projs; do
    echo "In-Progress tickets on project #$p ($owner) and their claim liveness:"
    echo
    "$GH" project item-list "$p" --owner "$owner" --limit 1000 --format json 2>/dev/null \
      | "$JQ" -r '.items[] | select(.status=="In Progress")
                  | [(.content.number // "-"), (.content.repository // "-"),
                     (.content.title // .title // "?")] | @tsv' \
      | while IFS=$'\t' read -r num repo title; do
          [ -n "$title" ] || continue
          if [ "$num" = "-" ]; then
            printf '— (draft)  %s\n   (draft note — no claim tracking)\n\n' "${title:0:70}"
            continue
          fi
          printf '— #%s  %s\n   %s\n   ' "$num" "${title:0:70}" "$repo"
          REPO="$repo" cmd_check "$num" || true
          echo
        done
    echo
  done
}

# --- arg parse ---------------------------------------------------------------
REPO=""; PROJECT=""; OWNER=""; ALLPROJ=""
SUB="${1:-}"; shift || true
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --project) PROJECT="$2"; shift 2;;
    --all-projects) ALLPROJ=1; shift;;
    --owner) OWNER="$2"; shift 2;;
    --) shift; POS+=("$@"); break;;
    *) POS+=("$1"); shift;;
  esac
done
set -- "${POS[@]:-}"

case "$SUB" in
  whoami)     cmd_whoami;;
  check)      [ -n "${1:-}" ] || { echo "usage: agent_claims.sh check <issue#>" >&2; exit 2; }; cmd_check "$1";;
  todo)       cmd_todo;;
  list)       cmd_list;;
  claim)      [ -n "${1:-}" ] || { echo "usage: agent_claims.sh claim <issue#>" >&2; exit 2; }; cmd_claim "$1";;
  release)    [ -n "${1:-}" ] || { echo "usage: agent_claims.sh release <issue#> [reason]" >&2; exit 2; }; cmd_release "$@";;
  set-status) [ -n "${2:-}" ] || { echo "usage: agent_claims.sh set-status <issue#> \"<Status>\"" >&2; exit 2; }; cmd_set_status "$1" "$2";;
  ""|-h|--help|help)
    sed -n '2,41p' "$0";;
  *) echo "unknown subcommand: $SUB (try: whoami check todo list claim release set-status)" >&2; exit 2;;
esac
