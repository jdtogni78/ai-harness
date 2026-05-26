#!/usr/bin/env bash
# Migrate this repo/dir from `ai-harness` -> `ai-harness`.
#
# Why a script (and why you run it from OUTSIDE the repo): the rename moves the
# very directory a Claude session may be running inside, restarts the launchd
# daemons, and renames the GitHub repo. Doing it by hand mid-session breaks the
# session's git linkage and the daemons. This script does the whole sequence as
# one unit, idempotently, with a dry-run first.
#
#   bash ~/dev/ai-harness/scripts/migrate_rename.sh            # dry-run (default)
#   bash ~/dev/ai-harness/scripts/migrate_rename.sh --apply    # do it
#   ...                                                  --apply --rename-labels
#
# Run it on EACH host that has a checkout (mini=user, note=user2).
# The first host edits + commits + pushes the shared repo files and renames the
# GitHub repo; the second host just pulls, moves its own dir, and reinstalls.
# Safe to re-run: every step checks whether it is already done.
#
# What it deliberately does NOT rename:
#   - the `claude remote-control` CLI subcommand (Anthropic's, has a space),
#   - the `remote-control-dirs` skill (it's about that CLI feature, not the repo),
#   - the `com.<user>.claude-usage-limit-monitor` label ("claude" = the product),
#   - the `com.<user>.claude-remote-control` launchd labels UNLESS --rename-labels.
set -euo pipefail

# --- self-relocate: copy to /tmp and re-exec so the `mv` can't pull the script
#     file out from under a running bash (the script lives inside the moved dir).
if [[ "${RENAME_RELOCATED:-}" != "1" ]]; then
  _tmp="$(mktemp -t migrate_rename.XXXXXX)"
  cp "$0" "$_tmp"
  exec env RENAME_RELOCATED=1 bash "$_tmp" "$@"
fi
cd "$HOME"   # never sit inside the dir we're about to move

OWNER="youruser"
OLD_NAME="claude-remote-control"
NEW_NAME="ai-harness"
OLD_DIR="$HOME/dev/$OLD_NAME"
NEW_DIR="$HOME/dev/$NEW_NAME"

APPLY=0
RENAME_LABELS=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --rename-labels) RENAME_LABELS=1 ;;
    -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# Resolve tools that may live off-PATH (launchd/cron bare env).
GH="$(command -v gh || echo /opt/homebrew/bin/gh)"
GIT="$(command -v git || echo /usr/bin/git)"
PY="/usr/bin/python3"
USER_NAME="$(id -un)"
UID_NUM="$(id -u)"
LA_DIR="$HOME/Library/LaunchAgents"

# Repo is wherever it currently lives (old name until moved, new after).
if [[ -d "$NEW_DIR/.git" ]]; then REPO="$NEW_DIR"
elif [[ -d "$OLD_DIR/.git" ]]; then REPO="$OLD_DIR"
else echo "FATAL: no checkout at $OLD_DIR or $NEW_DIR" >&2; exit 1; fi

say()  { printf '\n=== %s\n' "$*"; }
do_()  { if [[ $APPLY -eq 1 ]]; then echo "+ $*"; eval "$*"; else echo "DRYRUN  $*"; fi; }
note() { printf '        %s\n' "$*"; }

say "migrate $OLD_NAME -> $NEW_NAME   (host=$USER_NAME, apply=$APPLY, rename-labels=$RENAME_LABELS)"
note "repo currently at: $REPO"

# ---------------------------------------------------------------- preflight
say "preflight"
# Must have nothing uncommitted that isn't part of this migration.
if [[ -n "$("$GIT" -C "$REPO" status --porcelain)" ]]; then
  if [[ $APPLY -eq 1 ]]; then
    echo "ABORT: working tree at $REPO is dirty. Commit/stash unrelated changes first:" >&2
    "$GIT" -C "$REPO" status --short >&2
    exit 1
  fi
  note "WARNING: working tree at $REPO is dirty (would ABORT under --apply)"
else
  note "git tree clean: ok"
fi
note "$("$GIT" -C "$REPO" worktree list | wc -l | tr -d ' ') worktree entries will be repaired after the move"

# Did the shared repo files already get migrated (we are the 2nd host or a re-run)?
REPO_MIGRATED=0
if "$GIT" -C "$REPO" grep -q "dev/$NEW_NAME" -- remote_control/config.py 2>/dev/null; then
  REPO_MIGRATED=1
  note "shared repo files already migrated (config.py points at $NEW_NAME)"
fi

# --------------------------------------------------------- 1. stop daemons
say "1. stop this host's launchd daemons"
for label in "com.$USER_NAME.$OLD_NAME" "com.$USER_NAME.claude-usage-limit-monitor"; do
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    do_ "launchctl bootout gui/$UID_NUM/$label || true"
  else
    note "$label not loaded; skip"
  fi
done

# ------------------------------------------------ 2. rename the GitHub repo
say "2. rename GitHub repo (global; first host only)"
if "$GH" repo view "$OWNER/$NEW_NAME" >/dev/null 2>&1; then
  note "$OWNER/$NEW_NAME already exists on GitHub; skip"
elif "$GH" repo view "$OWNER/$OLD_NAME" >/dev/null 2>&1; then
  do_ "$GH repo rename $NEW_NAME -R $OWNER/$OLD_NAME --yes"
else
  note "neither repo visible via gh (auth?); set it manually if needed"
fi
do_ "$GIT -C \"$REPO\" remote set-url origin https://github.com/$OWNER/$NEW_NAME.git"

# --------------------------------------- 3. (2nd host) pull migrated content
if [[ $REPO_MIGRATED -eq 1 ]]; then
  say "3. pull already-migrated content (2nd host)"
  do_ "$GIT -C \"$REPO\" pull --ff-only"
fi

# --------------------------------------------------- 4. move the directory
say "4. move the working directory"
if [[ -d "$NEW_DIR/.git" ]]; then
  note "$NEW_DIR already exists; skip mv"
elif [[ -d "$OLD_DIR/.git" ]]; then
  do_ "mv \"$OLD_DIR\" \"$NEW_DIR\""
  if [[ $APPLY -eq 1 ]]; then REPO="$NEW_DIR"; fi   # dry-run keeps REPO=OLD for previews
fi

# ------------------------------------------------- 5. repair the worktrees
# Pass each worktree's NEW path explicitly. Renaming the parent dir moves every
# linked worktree with it, so their admin pointers all go stale at once; a bare
# `worktree repair` (no args) can only fix worktrees git can still locate via
# those pointers -- i.e. none of the moved ones -- silently leaving them broken.
say "5. repair worktree gitdir pointers (all $( "$GIT" -C "$REPO" worktree list 2>/dev/null | wc -l | tr -d ' ') entries)"
do_ "$GIT -C \"$NEW_DIR\" worktree repair \"$NEW_DIR\"/.claude/worktrees/*/"

# ------------------------------------- 6. edit shared repo files (1st host)
if [[ $REPO_MIGRATED -eq 0 ]]; then
  say "6. rewrite in-repo path/name references (1st host)"
  R="$NEW_DIR"; [[ -d "$NEW_DIR/.git" ]] || R="$REPO"   # dry-run: dir not moved yet
  # mapfile of tracked files (NUL-safe enough for our paths).
  files() { "$GIT" -C "$R" ls-files; }
  sed_all() {  # sed_all <expr>  -> apply to every tracked text file
    local expr="$1"
    if [[ $APPLY -eq 1 ]]; then
      ( cd "$R" && files | while IFS= read -r f; do
          [[ -f "$f" ]] && sed -i '' -e "$expr" "$f"
        done )
    else
      echo "DRYRUN  sed -i '' -e '$expr'  (all tracked files)"
    fi
  }
  sed_one() {  # sed_one <file> <expr>
    if [[ $APPLY -eq 1 ]]; then ( cd "$R" && sed -i '' -e "$2" "$1" )
    else echo "DRYRUN  sed -i '' -e '$2'  $1"; fi
  }

  # All subs below are in forms that NEVER collide with the
  # `com.<user>.claude-remote-control` labels or the `claude remote-control`
  # CLI (space), so they're safe as global subs -- and being global makes them
  # robust to main having added files since this script was written.
  # (a) absolute paths + GitHub URL.
  sed_all "s|dev/$OLD_NAME|dev/$NEW_NAME|g"
  sed_all "s|$OWNER/$OLD_NAME|$OWNER/$NEW_NAME|g"
  # (b) prose repo-name mentions: backtick-wrapped (skill board-maps), and the
  #     ' supervisor' / ' host' descriptors. None can match a com.* label.
  sed_all "s|\`$OLD_NAME\`|\`$NEW_NAME\`|g"
  sed_all "s|$OLD_NAME supervisor|$NEW_NAME supervisor|g"
  sed_all "s|$OLD_NAME host|$NEW_NAME host|g"
  # (c) one-offs: active-dirs.txt basename entry, README H1.
  sed_one active-dirs.txt "s|^$OLD_NAME@|$NEW_NAME@|"
  sed_one README.md "s|^# $OLD_NAME$|# $NEW_NAME|"

  # (d) optional: rename the launchd labels + plist files.
  if [[ $RENAME_LABELS -eq 1 ]]; then
    say "6d. rename launchd labels + plist files"
    for u in user user2; do
      sed_all "s|com.$u.$OLD_NAME|com.$u.$NEW_NAME|g"
      if [[ $APPLY -eq 1 && -f "$R/com.$u.$OLD_NAME.plist" ]]; then
        do_ "$GIT -C \"$R\" mv com.$u.$OLD_NAME.plist com.$u.$NEW_NAME.plist"
      else
        note "would git mv com.$u.$OLD_NAME.plist -> com.$u.$NEW_NAME.plist"
      fi
    done
    # stale installed copy of the OLD-label plist (this host only)
    do_ "rm -f \"$LA_DIR/com.$USER_NAME.$OLD_NAME.plist\""
  fi

  # leftover audit: any hyphenated repo-name refs that remain should be ONLY
  # the launchd labels/filenames (when not renaming labels).
  say "6e. remaining '$OLD_NAME' occurrences (expect only labels if not --rename-labels)"
  "$GIT" -C "$R" grep -n "$OLD_NAME" -- . ":(exclude)scripts/migrate_rename.sh" \
        ":(exclude)docs/rename-to-ai-harness.md" || note "none"

  say "6f. commit + push the rename"
  do_ "$GIT -C \"$R\" add -A"
  do_ "$GIT -C \"$R\" commit -m 'rename: $OLD_NAME -> $NEW_NAME'"
  do_ "$GIT -C \"$R\" push"
fi

# ------------------------------------------- 7. reinstall this host's agents
say "7. reinstall launchd agents from the new path"
do_ "(cd \"$NEW_DIR\" && PYTHONPATH=\"$NEW_DIR\" $PY -m remote_control install)"

# --------------------------------------------------------- 8. verify
say "8. verify"
note "worktrees now under: $("$GIT" -C "$NEW_DIR" worktree list 2>/dev/null | head -1)"
LBL="com.$USER_NAME.$OLD_NAME"; [[ $RENAME_LABELS -eq 1 ]] && LBL="com.$USER_NAME.$NEW_NAME"
if launchctl print "gui/$UID_NUM/$LBL" >/dev/null 2>&1; then
  launchctl print "gui/$UID_NUM/$LBL" | grep -E 'state|program' || true
else
  note "$LBL not running yet (check after --apply)"
fi
note "tail the daemon log: tail -n 20 \"$NEW_DIR/logs/launchd.out.log\""
note "run tests:           (cd \"$NEW_DIR\" && $PY -m unittest discover -s tests -t .)"

say "done (apply=$APPLY). If this was a dry-run, re-run with --apply."
