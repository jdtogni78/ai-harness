#!/usr/bin/env bash
# session_scan.sh — triage Claude Code sessions + git worktrees for a repo.
#
# Usage:
#   session_scan.sh sessions [PROJECT_DIR]   # per-session API-failure report
#   session_scan.sh worktrees [REPO]         # dirty / ahead-of-main worktrees
#   session_scan.sh rescue   [REPO] [OUTDIR] # dump diffs+untracked for dirty worktrees
#
# Defaults: REPO=cwd's repo root, PROJECT_DIR derived from it the way
# Claude Code slugifies paths ("/" -> "-").
set -euo pipefail

repo_root() { git -C "${1:-$PWD}" rev-parse --show-toplevel; }
proj_dir()  { echo "$HOME/.claude/projects/$(echo "$1" | sed 's#/#-#g')"; }

cmd=${1:-sessions}; shift || true

case "$cmd" in
sessions)
  REPO=$(repo_root "${1:-$PWD}")
  PD=$(proj_dir "$REPO")
  [ -d "$PD" ] || { echo "no project dir: $PD" >&2; exit 1; }
  printf '%-38s %-13s %-7s %-5s %s\n' SESSION ENDED APIERRS LAST? "CWD | FIRST PROMPT"
  for f in $(ls -t "$PD"/*.jsonl); do
    jq -rs --arg f "$(basename "${f%.jsonl}")" --arg r "$REPO" '
      (map(select(.cwd))|last|.cwd // "?") as $cwd
      | (map(select(.timestamp))|last|.timestamp // "?")[0:16] as $end
      | [ .[] | select(.message.model=="<synthetic>")
           | (.message.content[]?.text // "") | select(test("API Error";"i")) ] as $errs
      | ((map(select(.timestamp))|last) as $l
           | (($l.message.content[]?.text // "")|test("API Error";"i"))) as $eoe
      | ( [ .[] | select(.type=="user" and (.isMeta|not))
              | (.message.content | if type=="string" then . else (.[]?.text // "") end) ]
            | map(select(. != "" and (test("^<")|not))) | .[0] // "" )[0:60] as $p
      | "\($f)\t\($end)\t\($errs|length)\t\(if $eoe then "ERR" else "-" end)\t\($cwd|sub($r;"."))\t\($p)"
    ' "$f" 2>/dev/null \
    | awk -F'\t' '{printf "%-38s %-13s %-7s %-5s %s | %s\n",$1,$2,$3,$4,$5,$6}'
  done
  ;;
worktrees)
  REPO=$(repo_root "${1:-$PWD}")
  cd "$REPO"
  git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [ "$wt" = "$REPO" ] && continue
    br=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
    dirty=$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    ahead=$(git -C "$wt" rev-list --count "main..$br" 2>/dev/null || echo '?')
    # Content diff vs main's merge-base (3-dot): empty => branch adds nothing
    # to main even if `ahead` counts merge commits, i.e. already merged.
    stat=$(git -C "$wt" diff --shortstat "main...$br" 2>/dev/null \
            | sed -E 's/^ *//; s/ files? changed/f/; s/ insertions?\(\+\)/+/; s/ deletions?\(-\)/-/g; s/, / /g')
    if [ -z "$stat" ]; then state=MERGED; stat='-'; else state=UNMERGED; fi
    printf '%-46s br=%-26s dirty=%-4s ahead=%-4s %-9s diff=%s\n' \
      "${wt##*/}" "$br" "$dirty" "$ahead" "$state" "$stat"
  done
  ;;
rescue)
  REPO=$(repo_root "${1:-$PWD}")
  OUT=${2:-$HOME/.claude/worktree-rescue/$(date +%Y%m%d-%H%M%S)}
  mkdir -p "$OUT"
  cd "$REPO"
  git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [ "$wt" = "$REPO" ] && continue
    [ -z "$(git -C "$wt" status --porcelain 2>/dev/null)" ] && continue
    name=${wt##*/}
    git -C "$wt" diff HEAD > "$OUT/$name.patch" 2>/dev/null || true
    git -C "$wt" status --porcelain > "$OUT/$name.status"
    # copy untracked files preserving relative paths
    git -C "$wt" ls-files --others --exclude-standard | while read -r u; do
      mkdir -p "$OUT/$name.untracked/$(dirname "$u")"
      cp "$wt/$u" "$OUT/$name.untracked/$u"
    done
    echo "rescued $name -> $OUT/$name.{patch,status,untracked/}"
  done
  echo "OUTDIR: $OUT"
  ;;
*) echo "unknown cmd: $cmd" >&2; exit 2;;
esac
