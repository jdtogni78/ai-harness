#!/usr/bin/env bash
# link.sh — make this repo the single source of truth for the AppOne env
# pool scripts by symlinking the live paths under ~/.app-one-pool back here.
# Mirrors skills/link.sh. Idempotent: safe to re-run.
#
# CODE lives here (scripts/pool/); STATE stays in ~/.app-one-pool/
# (pool.tsv, testpool.tsv, the *.lock mutexes, testdb/ datadir, warm/ cache).
# The scripts reference $HOME/.app-one-pool for all state, so symlinking the
# code in place is transparent to every caller (agents + the test-env/lease-env
# skills, which invoke ~/.app-one-pool/{pool,testpool}.sh).
#
# Run from a STABLE checkout (not an ephemeral .claude worktree): the symlink
# points at wherever this file lives, so a worktree path would dangle on GC.
#
# Usage: scripts/pool/link.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POOL_HOME="$HOME/.app-one-pool"
mkdir -p "$POOL_HOME"

for name in pool.sh testpool.sh; do
  src="$SRC_DIR/$name"
  dest="$POOL_HOME/$name"
  [ -e "$src" ] || { echo "missing $src — skip"; continue; }
  # Already the correct symlink → leave it.
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
    echo "ok    $dest -> $src"
    continue
  fi
  # A real file → back it up before replacing, so live state is never lost.
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    bak="$dest.bak.$(date +%s)"
    mv "$dest" "$bak"
    echo "backed up $dest -> $bak"
  else
    rm -f "$dest"
  fi
  ln -s "$src" "$dest"
  echo "linked $dest -> $src"
done

echo "done."
