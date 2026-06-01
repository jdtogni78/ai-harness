#!/usr/bin/env bash
# link.sh — make this repo the single source of truth for an app's env-pool
# scripts by symlinking the live paths under the app's $STATE_DIR back here.
# Idempotent: safe to re-run.
#
# CODE lives here (scripts/pool/); STATE stays in $STATE_DIR (pool.tsv,
# testpool.tsv, the *.lock mutexes, testdb/ datadir, warm/ cache, and the
# app's config.local.yaml).
#
# IMPORTANT: the dispatchers + adapters resolve their siblings relative to the
# directory they are *invoked* from (i.e. $STATE_DIR, since `$0` is the
# symlink there, not its target). So the WHOLE code layout — dispatchers,
# sourced libs, AND the adapters — must be symlinked into $STATE_DIR, not just
# the two dispatchers. A half-linked state dir is what silently breaks
# `pool.sh`/`testpool.sh` after a host swap (FamilyFund-v2-archive#1).
#
# $STATE_DIR (and the rest of the layout) comes from the pool config. Point at
# it the same way the dispatchers do — either drop a scripts/pool/config.local.yaml
# first, or pass the config explicitly:
#
#   AI_HARNESS_POOL_CONFIG=~/.ai-harness/pools/<app>/config.yaml scripts/pool/link.sh
#
# Run from a STABLE checkout (not an ephemeral .claude worktree): the symlinks
# point at wherever this file lives, so a worktree path would dangle on GC.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the app config -> shell vars (STATE_DIR, NAME, ...). load-config.sh
# also exports AI_HARNESS_POOL_CONFIG to the file it actually used.
source "$SRC_DIR/load-config.sh"
POOL_HOME="${STATE_DIR:?load-config.sh did not set STATE_DIR — check the config}"
mkdir -p "$POOL_HOME/adapters"

# The complete code layout the dispatchers expect to find under $STATE_DIR.
FILES=(
  pool.sh
  testpool.sh
  load-config.sh
  pool-core.sh
  _yaml_to_env.py
  adapters/laravel-docker-pool.sh
  adapters/laravel-docker-testpool.sh
)

link_one() {
  local rel="$1" src="$SRC_DIR/$rel" dest="$POOL_HOME/$rel"
  [ -e "$src" ] || { echo "missing  $src — skip"; return; }
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
    echo "ok       $dest"
    return
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    local bak="$dest.bak.$(date +%s)"
    mv "$dest" "$bak"
    echo "backed up $dest -> $bak"
  else
    rm -f "$dest"
  fi
  ln -s "$src" "$dest"
  echo "linked   $dest -> $src"
}

for rel in "${FILES[@]}"; do link_one "$rel"; done

# config.local.yaml is app-specific STATE, not repo code — never invent it.
# If we resolved a config from $AI_HARNESS_POOL_CONFIG and the state dir has
# none yet, symlink it so the dispatchers (which look for
# $STATE_DIR/config.local.yaml) find it with no env var set.
dest_cfg="$POOL_HOME/config.local.yaml"
if [ ! -e "$dest_cfg" ] && [ -n "${AI_HARNESS_POOL_CONFIG:-}" ]; then
  ln -s "$AI_HARNESS_POOL_CONFIG" "$dest_cfg"
  echo "linked   $dest_cfg -> $AI_HARNESS_POOL_CONFIG"
elif [ ! -e "$dest_cfg" ]; then
  echo "note: no $dest_cfg — symlink it to your app config so pool.sh finds it:"
  echo "      ln -s ~/.ai-harness/pools/<app>/config.yaml $dest_cfg"
fi

echo "done. ($POOL_HOME)"
