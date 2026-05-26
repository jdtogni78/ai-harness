#!/usr/bin/env bash
# link.sh — make every skill in this dir available globally to BOTH the
# Claude Code CLI (~/.claude/skills) and the Codex CLI (~/.codex/skills) by
# symlinking each one back to this repo. Idempotent: safe to re-run.
#
# This repo's skills/ is the single source of truth; the global dirs just point
# here, so editing a skill once updates it for both tools on this machine.
#
# Usage: skills/link.sh
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGETS=("$HOME/.claude/skills" "$HOME/.codex/skills")

for base in "${TARGETS[@]}"; do
  mkdir -p "$base"
  for src in "$SKILLS_DIR"/*/; do
    name="$(basename "$src")"
    dest="$base/$name"
    # Replace an existing real dir or stale symlink; leave correct links alone.
    if [ -L "$dest" ] && [ "$(readlink "$dest")" = "${src%/}" ]; then
      continue
    fi
    rm -rf "$dest"
    ln -s "${src%/}" "$dest"
    echo "linked $dest -> ${src%/}"
  done
done

# Back-compat: some older callers reference a bare session_scan.sh path.
SCAN="$SKILLS_DIR/session-triage/scripts/session_scan.sh"
for compat in "$HOME/.claude/session_scan.sh" "$HOME/.codex/session_scan.sh"; do
  if [ -e "$SCAN" ]; then
    rm -f "$compat"
    ln -s "$SCAN" "$compat"
  fi
done

echo "done."
