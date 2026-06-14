#!/usr/bin/env bash
# Symlink dispatcher anchor CLAUDE.md files from this repo into the user's
# home dev anchors. Idempotent: if a target is already the correct symlink,
# no-op. If a real file is in the way, back it up exactly once before
# replacing it with the symlink.
#
# Usage: scripts/deploy-dispatcher-claudemd.sh [--dry-run]
#
# Does NOT reload/restart any service — dispatcher sessions re-read their
# anchor CLAUDE.md per session, so the next new session picks up changes.

set -euo pipefail

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/dispatcher-claudemd"

# target -> source (relative to $HOME for the target, absolute for the source)
declare -a MAPPINGS=(
    "dev/CLAUDE.md:$SRC_DIR/dev.CLAUDE.md"
)

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

deploy_one() {
    local rel_target="$1" src="$2"
    local target="$HOME/$rel_target"

    if [[ ! -f "$src" ]]; then
        echo "ERROR: source missing: $src" >&2
        return 1
    fi

    if [[ -L "$target" ]]; then
        local current
        current="$(readlink "$target")"
        if [[ "$current" == "$src" ]]; then
            echo "ok: $target -> $src (already correct)"
            return 0
        fi
        echo "replace: $target currently -> $current"
        run ln -snf "$src" "$target"
        return 0
    fi

    if [[ -e "$target" ]]; then
        local backup="$target.pre-symlink.bak"
        if [[ -e "$backup" ]]; then
            echo "backup already exists at $backup; leaving it and replacing target"
        else
            echo "backing up existing $target -> $backup"
            run cp -a "$target" "$backup"
        fi
        run ln -snf "$src" "$target"
        return 0
    fi

    # nothing there yet
    run mkdir -p "$(dirname "$target")"
    run ln -snf "$src" "$target"
}

for mapping in "${MAPPINGS[@]}"; do
    rel_target="${mapping%%:*}"
    src="${mapping#*:}"
    deploy_one "$rel_target" "$src"
done

echo
echo "Done. Verify with:"
for mapping in "${MAPPINGS[@]}"; do
    rel_target="${mapping%%:*}"
    echo "  readlink \$HOME/$rel_target"
done
