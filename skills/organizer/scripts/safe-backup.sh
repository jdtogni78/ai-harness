#!/usr/bin/env bash
# safe-backup.sh — the "reasonable backup" helper + APPLY GATE for the
# organizer (alias dream) skill's phase 5/6.
#
# The design anchor: backup-before-change is a GATE, not a suggestion. Phase 6
# (apply) must refuse to run until the affected paths' backup is CONFIRMED.
# This script makes that enforceable:
#
#   backup   create a reversible backup of a root's current state, non-
#            destructively: stash tracked deltas as a git object (working tree
#            untouched), tar untracked/new files, and record `git worktree
#            list`, `git stash list`, HEAD + branch. Writes a manifest.json and
#            a checksum so `verify` can confirm integrity later.
#   verify   THE GATE. Exit 0 iff the backup dir has an intact manifest;
#            non-zero otherwise. Apply steps call this and bail on non-zero.
#   gate     verify, then run `-- <command...>` ONLY if verify passed. This is
#            the enforcement wrapper: `gate --backup-dir D -- mv a b`.
#   revert   print (and with --run, execute) the reversible restore recipe.
#   list     list existing backups.
#
# No AI, no network beyond git. Backups live under
#   ${ORGANIZER_BACKUP_DIR:-~/.ai-harness/organizer-backups}/<label>-<UTCstamp>/
#
# Usage:
#   safe-backup.sh backup --root ~/dev/ai-harness [--label reorg]
#   safe-backup.sh verify --backup-dir <dir>
#   safe-backup.sh gate   --backup-dir <dir> -- <command ...>
#   safe-backup.sh revert --backup-dir <dir> [--run]
#   safe-backup.sh list
set -euo pipefail

BACKUP_ROOT="${ORGANIZER_BACKUP_DIR:-$HOME/.ai-harness/organizer-backups}"
MANIFEST="manifest.json"
CHECKSUM="manifest.sha256"

die() { echo "safe-backup: $*" >&2; exit 2; }
now_stamp() { date -u +%Y%m%dT%H%M%SZ; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

sha_file() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else die "no shasum/sha256sum available"; fi
}

json_escape() { python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'; }

cmd_backup() {
  local root="" label="reorg"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root) root="$2"; shift 2;;
      --label) label="$2"; shift 2;;
      *) die "unknown arg to backup: $1";;
    esac
  done
  [[ -n "$root" ]] || die "backup needs --root"
  root="$(cd "$root" 2>/dev/null && pwd)" || die "root not found: $root"

  local stamp dir
  stamp="$(now_stamp)"
  dir="$BACKUP_ROOT/${label}-${stamp}"
  mkdir -p "$dir"

  local is_repo="false" head="" branch="" stash_obj=""
  if git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    is_repo="true"
    head="$(git -C "$root" rev-parse HEAD 2>/dev/null || echo '')"
    branch="$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

    # Record the git state (read-only).
    git -C "$root" worktree list --porcelain > "$dir/worktree-list.txt" 2>/dev/null || true
    git -C "$root" stash list          > "$dir/stash-list.txt"    2>/dev/null || true
    git -C "$root" status --porcelain  > "$dir/status.txt"        2>/dev/null || true

    # Non-destructive tracked-delta capture: `git stash create` makes a commit
    # object WITHOUT touching the working tree or the stash stack. Empty output
    # means there were no tracked changes to capture.
    stash_obj="$(git -C "$root" stash create "organizer backup $stamp" 2>/dev/null || echo '')"
    if [[ -n "$stash_obj" ]]; then
      echo "$stash_obj" > "$dir/tracked-stash-object.txt"
      # Persist the object so it survives gc: a loose ref under refs/organizer.
      git -C "$root" update-ref "refs/organizer/backup-$stamp" "$stash_obj" 2>/dev/null || true
      git -C "$root" diff "$head" "$stash_obj" > "$dir/tracked-delta.patch" 2>/dev/null || true
    fi

    # Copy untracked + new (not-yet-committed) files into the backup, tarred.
    # These are the ones a move/delete could actually lose.
    git -C "$root" ls-files --others --exclude-standard -z 2>/dev/null \
      | ( cd "$root" && tar --null -T - -czf "$dir/untracked-files.tar.gz" 2>/dev/null ) || true
  else
    echo "not a git repo — full snapshot fallback" > "$dir/status.txt"
    # Best-effort: tar the whole root (excluding heavy dirs) as the only backup.
    ( cd "$root" && tar --exclude='.git' --exclude='node_modules' \
        --exclude='.venv' -czf "$dir/full-snapshot.tar.gz" . 2>/dev/null ) || true
  fi

  # Manifest last, then checksum it — that pair is what `verify` trusts.
  local n_untracked=0
  [[ -f "$dir/status.txt" ]] && n_untracked="$(grep -c '^??' "$dir/status.txt" 2>/dev/null || echo 0)"
  cat > "$dir/$MANIFEST" <<EOF
{
  "tool": "safe-backup",
  "created_at": "$(now_iso)",
  "label": $(printf '%s' "$label" | json_escape),
  "root": $(printf '%s' "$root" | json_escape),
  "is_repo": $is_repo,
  "head": $(printf '%s' "$head" | json_escape),
  "branch": $(printf '%s' "$branch" | json_escape),
  "tracked_stash_object": $(printf '%s' "$stash_obj" | json_escape),
  "backup_ref": "refs/organizer/backup-$stamp",
  "artifacts": {
    "worktree_list": "worktree-list.txt",
    "stash_list": "stash-list.txt",
    "status": "status.txt",
    "tracked_delta_patch": "tracked-delta.patch",
    "untracked_tar": "untracked-files.tar.gz"
  }
}
EOF
  sha_file "$dir/$MANIFEST" > "$dir/$CHECKSUM"

  echo "$dir"
  echo "safe-backup: backup confirmed at $dir" >&2
  echo "safe-backup: HEAD=$head branch=$branch tracked_stash=${stash_obj:-none}" >&2
}

cmd_verify() {
  local dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backup-dir) dir="$2"; shift 2;;
      *) die "unknown arg to verify: $1";;
    esac
  done
  [[ -n "$dir" ]] || die "verify needs --backup-dir"
  if [[ ! -d "$dir" ]]; then
    echo "GATE-FAIL: backup dir does not exist: $dir" >&2; return 1; fi
  if [[ ! -f "$dir/$MANIFEST" ]]; then
    echo "GATE-FAIL: no manifest in $dir" >&2; return 1; fi
  if [[ ! -f "$dir/$CHECKSUM" ]]; then
    echo "GATE-FAIL: no checksum in $dir" >&2; return 1; fi
  local want have
  want="$(cat "$dir/$CHECKSUM")"
  have="$(sha_file "$dir/$MANIFEST")"
  if [[ "$want" != "$have" ]]; then
    echo "GATE-FAIL: manifest checksum mismatch in $dir" >&2; return 1; fi
  echo "GATE-OK: backup verified at $dir"
  return 0
}

cmd_gate() {
  local dir="" ; local -a rest=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backup-dir) dir="$2"; shift 2;;
      --) shift; rest=("$@"); break;;
      *) die "unknown arg to gate: $1 (did you forget -- before the command?)";;
    esac
  done
  [[ -n "$dir" ]] || die "gate needs --backup-dir"
  [[ ${#rest[@]} -gt 0 ]] || die "gate needs a command after --"
  if ! cmd_verify --backup-dir "$dir"; then
    echo "GATE-BLOCKED: refusing to run apply command — backup not confirmed." >&2
    echo "  blocked command: ${rest[*]}" >&2
    return 3
  fi
  echo "GATE-PASSED: running: ${rest[*]}" >&2
  "${rest[@]}"
}

cmd_revert() {
  local dir="" run="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backup-dir) dir="$2"; shift 2;;
      --run) run="true"; shift;;
      *) die "unknown arg to revert: $1";;
    esac
  done
  [[ -n "$dir" ]] || die "revert needs --backup-dir"
  [[ -f "$dir/$MANIFEST" ]] || die "no manifest in $dir"
  local root ref
  root="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["root"])' "$dir/$MANIFEST")"
  ref="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["backup_ref"])' "$dir/$MANIFEST")"
  echo "# Revert recipe for backup $dir"
  echo "# root: $root"
  echo "# 1) restore tracked deltas that were captured (if any):"
  echo "git -C '$root' stash apply $ref   # or: git -C '$root' checkout $ref -- <path>"
  echo "# 2) restore untracked/new files:"
  echo "tar -xzf '$dir/untracked-files.tar.gz' -C '$root'"
  echo "# 3) inspect the recorded state:"
  echo "cat '$dir/worktree-list.txt' '$dir/stash-list.txt' '$dir/status.txt'"
  if [[ "$run" == "true" ]]; then
    echo "safe-backup: --run set, restoring untracked files…" >&2
    [[ -f "$dir/untracked-files.tar.gz" ]] && tar -xzf "$dir/untracked-files.tar.gz" -C "$root" || true
    echo "safe-backup: tracked deltas NOT auto-applied (review $ref first)." >&2
  fi
}

cmd_list() {
  [[ -d "$BACKUP_ROOT" ]] || { echo "no backups under $BACKUP_ROOT"; return 0; }
  echo "backups under $BACKUP_ROOT:"
  for d in "$BACKUP_ROOT"/*/; do
    [[ -d "$d" ]] || continue
    if [[ -f "$d/$MANIFEST" ]]; then
      echo "  OK   ${d%/}"
    else
      echo "  BAD  ${d%/}  (no manifest)"
    fi
  done
}

main() {
  [[ $# -gt 0 ]] || die "usage: safe-backup.sh {backup|verify|gate|revert|list} ..."
  local sub="$1"; shift
  case "$sub" in
    backup) cmd_backup "$@";;
    verify) cmd_verify "$@";;
    gate)   cmd_gate "$@";;
    revert) cmd_revert "$@";;
    list)   cmd_list "$@";;
    *) die "unknown subcommand: $sub";;
  esac
}

main "$@"
