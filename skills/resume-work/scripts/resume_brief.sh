#!/usr/bin/env bash
# resume_brief.sh — reconstruct a resumable briefing from ONE Claude Code
# session transcript + the worktree it ran in. Read-only; never mutates git.
#
# Usage:
#   resume_brief.sh [list] [--all] [N]   list resumable sessions for this repo.
#                                        By default hides sessions whose work
#                                        is done (worktree gone AND branch
#                                        merged-or-missing) plus those you've
#                                        explicitly ack'd. --all shows every-
#                                        thing. N caps rows (default 15).
#   resume_brief.sh SELECTOR             brief one session, where SELECTOR is one of:
#       - a path to a .jsonl transcript
#       - a session UUID (matched as <uuid>.jsonl under ~/.claude/projects)
#       - a worktree dir name or path, or any substring of cwd / first prompt
#   resume_brief.sh ack   UUID [UUID...] mark sessions as handled (hidden from list)
#   resume_brief.sh unack UUID [UUID...] undo ack
#
# list  → numbered table: ended time, ERR (died on API error), worktree,
#         branch, dirty count, original goal, and the UUID to pass back.
# brief → transcript path, cwd/worktree, branch+dirty, git status, original
#         goal, recent prompts, last assistant text, last TodoWrite list,
#         files Edited/Written.
set -euo pipefail

PROJ="$HOME/.claude/projects"

# --- per-repo state: slug + ack file ----------------------------------------
# Anchor to the MAIN working tree even when invoked from a linked worktree,
# so the ack file is shared and the worktree-branch heuristic compares
# against the right ref set. `--git-common-dir` always points to the main
# repo's .git; its parent is the main working tree.
if common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null); then
  repo_root=$(dirname "$common")
else
  repo_root="$PWD"
fi
repo_slug=$(echo "$repo_root" | sed 's#/#-#g')
ack_file="$PROJ/$repo_slug/.resume-acked"

is_acked() {
  [ -f "$ack_file" ] || return 1
  grep -qx -- "$1" "$ack_file"
}

# is_resolved: cwd dir is gone AND the branch the worktree used (named
# `worktree-<basename>`) is either also gone (cleanup deleted it post-merge)
# or has zero unique content vs `main`. Conservative — only returns 0 if
# we're sure. Sessions in unconventional cwds (main repo, claude-remote-
# control, etc.) never get auto-hidden.
is_resolved() {
  local cwd="$1" base br
  [ -d "$cwd" ] && return 1
  base="${cwd##*/}"
  case "$base" in
    bridge-cse_*|worktree-*) br="worktree-${base#worktree-}";;
    *) return 1;;
  esac
  if ! git -C "$repo_root" rev-parse --verify "refs/heads/$br" >/dev/null 2>&1; then
    return 0
  fi
  [ -z "$(git -C "$repo_root" diff --shortstat "main...$br" 2>/dev/null)" ] && return 0
  return 1
}

# --- candidate transcripts for the current repo (newest first) --------------
# The second glob (`"$base"*/*.jsonl`) intentionally widens to subdir-projects
# (worktrees, app1, family-fund-app under the same repo) but it ALSO matches
# the exact base dir itself — hence the awk dedupe.
gather_cands() {
  base="$PROJ/$repo_slug"
  cands=()
  while IFS= read -r line; do cands+=("$line"); done < <(ls -t "$base"/*.jsonl "$base"*/*.jsonl 2>/dev/null | awk '!seen[$0]++')
  if [ "${#cands[@]}" -eq 0 ]; then
    while IFS= read -r line; do cands+=("$line"); done < <(ls -t "$PROJ"/*/*.jsonl 2>/dev/null)
  fi
}

# --- ack / unack ------------------------------------------------------------
case "${1:-}" in
  ack|unack)
    op="$1"; shift
    [ $# -eq 0 ] && { echo "usage: resume_brief.sh $op UUID [UUID...]" >&2; exit 2; }
    mkdir -p "$(dirname "$ack_file")"
    touch "$ack_file"
    for u in "$@"; do
      if [ "$op" = "ack" ]; then
        if grep -qx -- "$u" "$ack_file"; then echo "already acked: $u"
        else echo "$u" >> "$ack_file"; echo "acked: $u"; fi
      else
        if grep -qx -- "$u" "$ack_file"; then
          # `|| true` because grep -vx exits 1 when no lines remain, and we
          # still want to keep going under `set -e` to overwrite with empty.
          { grep -vx -- "$u" "$ack_file" || true; } > "$ack_file.tmp"
          mv "$ack_file.tmp" "$ack_file"
          echo "unacked: $u"
        else echo "not acked: $u"; fi
      fi
    done
    exit 0
    ;;
esac

# --- list mode (default when no selector) -----------------------------------
if [ $# -eq 0 ] || [ "${1:-}" = "list" ]; then
  [ "${1:-}" = "list" ] && shift
  show_all=0
  if [ "${1:-}" = "--all" ]; then show_all=1; shift; fi
  limit="${1:-15}"
  gather_cands
  [ "${#cands[@]}" -eq 0 ] && { echo "no sessions found for this repo" >&2; exit 1; }
  printf '%-3s %-13s %-4s %-34s %-9s %s\n' "#" ENDED ERR WORKTREE DIRTY "GOAL  ( pass UUID back )"
  shown=0; hidden_done=0; hidden_acked=0
  for f in "${cands[@]}"; do
    uuid=$(basename "${f%.jsonl}")
    cwd=$(jq -rs 'map(select(.cwd))|last|.cwd // "?"' "$f" 2>/dev/null)
    if [ "$show_all" -eq 0 ]; then
      if is_acked "$uuid"; then hidden_acked=$((hidden_acked+1)); continue; fi
      if is_resolved "$cwd"; then hidden_done=$((hidden_done+1)); continue; fi
    fi
    shown=$((shown+1)); [ "$shown" -gt "$limit" ] && break
    end=$(jq -rs '(map(select(.timestamp))|last|.timestamp // "?")[0:16]' "$f" 2>/dev/null)
    eoe=$(jq -rs '(map(select(.timestamp))|last) as $l|(($l.message.content[]?.text // "")|test("API Error";"i"))' "$f" 2>/dev/null || echo false)
    goal=$(jq -rs '[.[]|select(.type=="user" and (.isMeta|not))|(.message.content|if type=="string" then . else (.[]?.text//"") end)]|map(select(.!="" and (test("^<")|not)))|.[0]//""' "$f" 2>/dev/null)
    err=$([ "$eoe" = "true" ] && echo "ERR" || echo "-")
    if [ -d "$cwd" ] && git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
      dirty=$(git -C "$cwd" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    else dirty="gone"; fi
    printf '%-3s %-13s %-4s %-34s %-9s %s\n' \
      "$shown" "$end" "$err" "${cwd##*/}" "$dirty" "${goal:0:70}"
    printf '    └ %s\n' "$uuid"
  done
  echo
  if [ "$show_all" -eq 0 ] && [ $((hidden_done + hidden_acked)) -gt 0 ]; then
    echo "(hidden: $hidden_done done (worktree gone + branch merged/missing), $hidden_acked ack'd. Pass --all to show; 'ack UUID' / 'unack UUID' to manage.)"
  fi
  echo "Pick one, then: resume_brief.sh <UUID>"
  exit 0
fi

sel="$1"

# --- resolve a single transcript --------------------------------------------
transcript=""
if [ -f "$sel" ]; then
  transcript="$sel"
elif ls "$PROJ"/*/"$sel".jsonl >/dev/null 2>&1; then
  transcript=$(ls -t "$PROJ"/*/"$sel".jsonl | head -1)
else
  gather_cands
  for f in "${cands[@]}"; do
    cwd=$(jq -rs 'map(select(.cwd))|last|.cwd // ""' "$f" 2>/dev/null)
    fp=$(jq -rs '[.[]|select(.type=="user" and (.isMeta|not))|(.message.content|if type=="string" then . else (.[]?.text//"") end)]|map(select(.!="" and (test("^<")|not)))|.[0]//""' "$f" 2>/dev/null)
    case "$cwd$fp" in *"$sel"*) transcript="$f"; break;; esac
  done
fi
[ -z "$transcript" ] || [ ! -f "$transcript" ] && { echo "no transcript resolved (selector: '$sel'). Run with no args to list." >&2; exit 1; }

# --- extract from transcript ------------------------------------------------
cwd=$(jq -rs 'map(select(.cwd))|last|.cwd // "?"' "$transcript")
end=$(jq -rs '(map(select(.timestamp))|last|.timestamp // "?")[0:16]' "$transcript")
eoe=$(jq -rs '(map(select(.timestamp))|last) as $l | (($l.message.content[]?.text // "")|test("API Error";"i"))' "$transcript")

echo "=== RESUME BRIEFING ==="
echo "transcript : $transcript"
echo "ended      : $end   (ended-on-API-error: $eoe)"
echo "cwd        : $cwd"

# --- worktree state ---------------------------------------------------------
if [ -d "$cwd" ] && git -C "$cwd" rev-parse --git-dir >/dev/null 2>&1; then
  br=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
  dirty=$(git -C "$cwd" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  echo "branch     : $br   (dirty files: $dirty)"
  [ "$dirty" != "0" ] && { echo "--- git status (worktree) ---"; git -C "$cwd" status --short 2>/dev/null | head -40; }
else
  echo "branch     : (cwd missing or not a git repo — worktree may have been removed)"
fi

echo
echo "--- ORIGINAL GOAL (first user prompt) ---"
jq -rs '[.[]|select(.type=="user" and (.isMeta|not))|(.message.content|if type=="string" then . else (.[]?.text//"") end)]|map(select(.!="" and (test("^<")|not)))|.[0]//"(none)"' "$transcript"

echo
echo "--- RECENT USER PROMPTS (last 5) ---"
jq -rs '[.[]|select(.type=="user" and (.isMeta|not))|(.message.content|if type=="string" then . else (.[]?.text//"") end)]|map(select(.!="" and (test("^<")|not)))[-5:]|to_entries[]|"  • \(.value)"' "$transcript"

echo
echo "--- LAST ASSISTANT MESSAGE ---"
jq -rs '[.[]|select(.type=="assistant")|.message.content]|map(.[]?|select(.type=="text")|.text)|map(select(.!=""))|last//"(none)"' "$transcript" | sed 's/^/  /'

echo
echo "--- LAST TODO LIST ---"
jq -rs '[.[]|.message.content[]?|select(.type=="tool_use" and .name=="TodoWrite")|.input.todos]|last' "$transcript" 2>/dev/null \
  | jq -r 'if .==null then "  (no TodoWrite calls in session)" else .[]|"  [\(.status|.[0:4])] \(.content)" end' 2>/dev/null \
  || echo "  (no TodoWrite calls in session)"

echo
echo "--- FILES EDITED/WRITTEN ---"
files=$(jq -rs '[.[]|.message.content[]?|select(.type=="tool_use" and (.name=="Edit" or .name=="Write" or .name=="NotebookEdit"))|.input.file_path]|unique|.[]?' "$transcript" 2>/dev/null || true)
if [ -n "$files" ]; then echo "$files" | sed 's/^/  /'; else echo "  (none)"; fi

echo
echo "=== END BRIEFING ==="
