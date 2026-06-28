#!/usr/bin/env bash
# take_over.sh — adopt a stale/disconnected session's work in a fresh session.
#
# Usage:
#   take_over.sh <UUID>
#
# Steps:
#   1. Run resume_brief.sh <UUID> to extract goal, last todo, files, last msg
#   2. Write the brief to a temp file
#   3. Spawn a new-session anchored at the old session's cwd (--prompt-file)
#   4. Ack the old session so it leaves the interrupted list
#
set -euo pipefail

RESUME_BRIEF="$(dirname "$0")/../../resume-work/scripts/resume_brief.sh"

usage() { echo "usage: take_over.sh <UUID>" >&2; exit 2; }
[ $# -eq 1 ] || usage
UUID="$1"

# --- 1. Extract the briefing -------------------------------------------------
echo "==> Generating brief for session $UUID ..."
brief_content=$("$RESUME_BRIEF" "$UUID" 2>&1) || {
  echo "ERROR: resume_brief.sh failed for UUID $UUID" >&2
  exit 1
}

# --- 2. Extract cwd from brief -----------------------------------------------
cwd=$(echo "$brief_content" | grep '^cwd ' | head -1 | sed 's/^cwd[[:space:]]*:[[:space:]]*//')
if [ -z "$cwd" ] || [ "$cwd" = "?" ]; then
  echo "ERROR: could not determine cwd from brief. Output was:" >&2
  echo "$brief_content" >&2
  exit 1
fi

if [ ! -d "$cwd" ]; then
  echo "WARNING: cwd '$cwd' no longer exists. The worktree may have been removed."
  echo "  Anchoring new session to its parent: $(dirname "$cwd")"
  cwd=$(dirname "$cwd")
fi

echo "==> cwd: $cwd"

# --- 3. Write prompt file ----------------------------------------------------
prompt_file=$(mktemp /tmp/take-over-brief-XXXXXX.md)
trap 'rm -f "$prompt_file"' EXIT

cat > "$prompt_file" <<PROMPT
# Take-over: resume stale session $UUID

The session below was interrupted or became stale. Your job is to pick up
exactly where it left off.

## Briefing

$brief_content

## Instructions

1. Review the original goal, last todo list, and files edited above.
2. Check current git status and reconcile with main — avoid redoing landed work.
3. Continue the task from the genuine remaining gap.
4. When done, close the work normally (commit, close-work, etc.).
PROMPT

# --- 4. Spawn the new session ------------------------------------------------
echo "==> Spawning new session anchored at $cwd ..."
python3 -m remote_control new-session \
  --dir "$cwd" \
  --prompt-file "$prompt_file"

# --- 5. Ack the old session --------------------------------------------------
echo "==> Acking old session $UUID ..."
"$RESUME_BRIEF" ack "$UUID"

echo "==> Done. Old session $UUID acked; new worker dispatched."
