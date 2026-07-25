#!/bin/bash
# answers.sh — the boss's durable "answers feed".
#
# When a manager (or the meta-manager) answers an EXPLICIT question from the
# boss, that answer scrolls away in a fast-moving chat and can't be found
# later. This script gives the answer a durable, single, low-noise home:
#
#   1. it PREPENDS the Q&A to the right `##` section of one iCloud Markdown
#      file (the source of truth), newest-first, under a mkdir lock so
#      concurrent managers can't corrupt it;
#   2. it MIRRORS the same Q&A into a durable, pinned "[INBOX] Boss answers
#      feed" session for in-app reading (recreated if missing);
#   3. it fires a NOTIFICATION (desktop via osascript here, and a phone push
#      via the inbox session's standing PushNotification instruction).
#
# One `post` call does all three so a caller can never half-post.
#
# Subcommands:
#   post --mgr MGR-N --subject "<title>" [--ticket N] --q "<question>" --a "<answer>"
#        [--no-inbox] [--no-notify]
#   file            print the iCloud answers-file path
#   inbox-id        print the inbox session cse_* (ensuring/creating it)
#   inbox-status    show the recorded inbox cse_* and whether it is live
#   path            print the answers state dir
#
# Env:
#   ANSWERS_FILE        override the iCloud file (tests point this at a tmp file)
#   ANSWERS_STATE_DIR   override state dir (default ~/.ai-harness/answers)
#   AI_HARNESS_DIR      ai-harness checkout (default ~/dev/ai-harness)
#   ANSWERS_NOW         override the entry timestamp (tests; else `date`)
#
# Conventions mirror skills/manage/scripts/workers.sh (arg parsing, $STATE_DIR,
# home-dir usage, mkdir-based lock — macOS ships no flock(1)).

set -euo pipefail

STATE_DIR="${ANSWERS_STATE_DIR:-$HOME/.ai-harness/answers}"
AI_HARNESS_DIR="${AI_HARNESS_DIR:-$HOME/dev/ai-harness}"
DEFAULT_ANSWERS_FILE="$HOME/Library/Mobile Documents/com~apple~CloudDocs/claude-answers/ANSWERS.md"
ANSWERS_FILE="${ANSWERS_FILE:-$DEFAULT_ANSWERS_FILE}"
# The title-watcher owns the FIRST bracket (`[NICK.host]`) and rewrites it every
# pass, so an `[INBOX]` first bracket would not survive. Carry the label as a
# sub-bracket instead: the watcher keeps `[NICK.host][INBOX] Boss answers feed`.
INBOX_SUB="INBOX"
INBOX_DESC="Boss answers feed"

die() { echo "answers.sh: $*" >&2; exit 2; }

now_stamp() {
  if [[ -n "${ANSWERS_NOW:-}" ]]; then printf '%s\n' "$ANSWERS_NOW"; else date +"%Y-%m-%d %H:%M"; fi
}

state_dir() {
  mkdir -p "$STATE_DIR" >/dev/null
  chmod 700 "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$STATE_DIR"
}

inbox_marker() { printf '%s/inbox.cse\n' "$(state_dir)"; }

# ---- file lock (mkdir is atomic on every POSIX fs; macOS has no flock(1)) ----
# Guards the read-modify-write on ANSWERS_FILE so two managers posting at once
# can't interleave and drop an entry. Same pattern workers.sh uses for the
# ordinals ledger (its TOCTOU saga is why this is not optional).
_answers_lock() {
  local lock deadline
  lock="${ANSWERS_FILE}.lock"
  deadline=$(( $(date +%s) + 10 ))
  while ! mkdir "$lock" 2>/dev/null; do
    if [[ $(date +%s) -ge $deadline ]]; then
      # Break a lock orphaned by a killed process rather than wedging forever.
      rmdir "$lock" 2>/dev/null || true
      mkdir "$lock" 2>/dev/null && break
      die "post: could not acquire $lock"
    fi
    sleep 0.1
  done
  _ANSWERS_LOCK="$lock"
  trap '_answers_unlock' EXIT INT TERM
}
_answers_unlock() {
  [[ -n "${_ANSWERS_LOCK:-}" ]] && rmdir "$_ANSWERS_LOCK" 2>/dev/null || true
  _ANSWERS_LOCK=""
}

# ---- the file mutation: prepend an entry within its subject section ----------
# Runs under the lock. Idempotent enough that a retried post is a no-op: if the
# exact entry block already exists verbatim, nothing is written. Existing
# entries are NEVER rewritten — we only prepend within a section, or create one.
_prepend_entry() {
  local section="$1" block="$2"
  SECTION="$section" BLOCK="$block" FILE="$ANSWERS_FILE" python3 - <<'PY'
import os, pathlib
path = pathlib.Path(os.environ["FILE"])
section = os.environ["SECTION"].rstrip("\n")       # "## MGR-13 — subject"
block = os.environ["BLOCK"].rstrip("\n") + "\n"     # "### ts ...\n**Q:**...\n**A:**...\n"

path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    text = path.read_text()
else:
    text = "# Boss Answers Feed\n\nDurable answers to the boss's questions. Newest first within each subject.\n"

# Idempotent: exact block already present -> no-op (retry-safe).
if block.strip() in text:
    print("duplicate")
    raise SystemExit(0)

lines = text.splitlines()

# Locate the section header (exact match on the stripped line).
sec_idx = next((i for i, ln in enumerate(lines) if ln.strip() == section), None)

if sec_idx is not None:
    # Insert immediately after the header (and its one blank line) => newest on
    # top within the section.
    ins = sec_idx + 1
    if ins < len(lines) and lines[ins].strip() == "":
        ins += 1
    new_block = block.rstrip("\n").split("\n") + [""]
    lines[ins:ins] = new_block
    result = "prepended"
else:
    # New section: place it just before the first EXISTING `## ` section (so the
    # most-recently-active subject floats to the top too); if there is none yet,
    # append at the end. This keeps it below the H1 title + its blurb.
    top = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    section_block = [section, ""] + block.rstrip("\n").split("\n") + ["", ""]
    # Guarantee a blank line before the new header when it follows content.
    if top > 0 and lines[top - 1].strip() != "":
        section_block = [""] + section_block
    lines[top:top] = section_block
    result = "created-section"

path.write_text("\n".join(lines).rstrip("\n") + "\n")
print(result)
PY
}

# ---- inbox session -----------------------------------------------------------
# The durable in-app mirror. Its standing first-turn brief tells it to render
# each posted answer cleanly AND fire ONE PushNotification (phone reach) and
# nothing else, so per-post messages carry only the Q&A block.
_inbox_first_turn_brief() {
  cat <<'BRIEF'
You are the **Boss Answers inbox** — a durable, low-noise feed. Your SOLE job:
each time you receive an answer block, do exactly three things and NOTHING else:

1. Render the answer block verbatim as clean Markdown (a `###` heading line, a
   **Q:** line, an **A:** line). Do not summarize, analyze, or comment.
2. Fire exactly ONE PushNotification, status "proactive", message like
   "New answer · <tag> · <question snippet>" (<200 chars, one line) so it
   reaches the boss's phone.
3. Output nothing further — no follow-up questions, no offers to help, no
   analysis. The boss reads a clean feed; keep it clean.

This is your standing instruction for every future message in this session.
BRIEF
}

# Return the inbox cse_*, creating the session if the marker is missing or its
# recorded session is no longer live. The iCloud file is the durable backup, so
# a lost inbox is recoverable, not catastrophic — we just recreate it.
_ensure_inbox() {
  local marker id
  marker="$(inbox_marker)"
  if [[ -s "$marker" ]]; then
    id="$(cat "$marker")"
    if _session_is_live "$id"; then printf '%s\n' "$id"; return 0; fi
    echo "answers.sh: inbox $id is no longer live; recreating" >&2
  fi
  # Spawn a fresh inbox session, seed it with the standing brief, title it.
  local out
  out="$(cd "$AI_HARNESS_DIR" && python3 -m remote_control new-session \
      --dir "$AI_HARNESS_DIR" --name "answers-inbox" --spawn same-dir \
      --wait --no-reply-to --prompt-file <(_inbox_first_turn_brief) 2>&1)" \
    || { echo "$out" >&2; die "inbox: new-session failed"; }
  id="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*session[[:space:]]*:[[:space:]]*//p' | tail -1)"
  [[ "$id" == cse_* ]] || { echo "$out" >&2; die "inbox: could not parse cse_* from new-session output"; }
  printf '%s\n' "$id" > "$marker"
  ( cd "$AI_HARNESS_DIR" && python3 -m remote_control titles set \
      --id "$id" --force-host --sub "$INBOX_SUB" "$INBOX_DESC" >/dev/null 2>&1 ) || true
  printf '%s\n' "$id"
}

_session_is_live() {
  local id="$1"
  [[ "$id" == cse_* ]] || return 1
  ( cd "$AI_HARNESS_DIR" && python3 -m remote_control sessions list --json 2>/dev/null ) \
    | python3 -c 'import json,sys; want=sys.argv[1]; print(any(r.get("id")==want for r in json.load(sys.stdin)))' "$id" \
    2>/dev/null | grep -q True
}

_post_to_inbox() {
  local block="$1" id
  id="$(_ensure_inbox)" || return 1
  ( cd "$AI_HARNESS_DIR" && python3 -m remote_control sessions submit "$id" \
      --stdin --no-reply-to >/dev/null ) <<<"$block" \
    || { echo "answers.sh: failed to submit to inbox $id" >&2; return 1; }
  printf '%s\n' "$id"
}

# ---- desktop notification (this Mac only; phone push rides the inbox brief) --
_notify_desktop() {
  local snippet="$1"
  command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display notification \"${snippet//\"/\\\"}\" with title \"Boss answers feed\"" \
    >/dev/null 2>&1 || true
}

cmd_post() {
  local mgr="" subject="" ticket="" q="" a="" do_inbox=true do_notify=true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mgr)      mgr="${2:-}";     shift 2 ;;
      --subject)  subject="${2:-}"; shift 2 ;;
      --ticket)   ticket="${2:-}";  shift 2 ;;
      --q)        q="${2:-}";       shift 2 ;;
      --a)        a="${2:-}";       shift 2 ;;
      --no-inbox)  do_inbox=false;  shift ;;
      --no-notify) do_notify=false; shift ;;
      *) die "post: unknown arg: $1" ;;
    esac
  done
  [[ -n "$mgr" ]]     || die "post: --mgr MGR-N is required"
  [[ -n "$subject" ]] || die "post: --subject \"<title>\" is required"
  [[ -n "$q" ]]       || die "post: --q \"<question>\" is required"
  [[ -n "$a" ]]       || die "post: --a \"<answer>\" is required"
  ticket="${ticket#\#}"

  local stamp tag section block
  stamp="$(now_stamp)"
  if [[ -n "$ticket" ]]; then tag="${mgr} / #${ticket}"; else tag="${mgr}"; fi
  section="## ${mgr} — ${subject}"
  block="$(printf '### %s · [%s]\n**Q:** %s\n**A:** %s\n' "$stamp" "$tag" "$q" "$a")"

  # 1) durable file, under lock. Ensure the parent dir exists first — the lock
  #    is a sibling dir of the file, so mkdir would fail if the tree is absent.
  mkdir -p "$(dirname "$ANSWERS_FILE")" >/dev/null
  local wrote
  _answers_lock
  wrote="$(_prepend_entry "$section" "$block")" || { _answers_unlock; die "post: file write failed"; }
  _answers_unlock
  echo "answers.sh: file[$ANSWERS_FILE] -> $wrote ($section)"

  # 2) mirror into the inbox session
  local inbox_id=""
  if $do_inbox; then
    inbox_id="$(_post_to_inbox "$block")" || echo "answers.sh: inbox mirror failed (file is still the durable record)" >&2
    [[ -n "$inbox_id" ]] && echo "answers.sh: mirrored -> inbox $inbox_id"
  fi

  # 3) notification — desktop here; phone push rides the inbox brief's
  #    standing PushNotification instruction (fired by the inbox agent).
  if $do_notify; then
    local snippet="New answer · [${tag}] · ${q}"
    snippet="${snippet:0:180}"
    _notify_desktop "$snippet"
    echo "answers.sh: desktop notification fired"
  fi
}

cmd_inbox_status() {
  local marker id
  marker="$(inbox_marker)"
  if [[ ! -s "$marker" ]]; then echo "inbox: none recorded (will be created on next post)"; return 0; fi
  id="$(cat "$marker")"
  if _session_is_live "$id"; then echo "inbox: $id (live)"; else echo "inbox: $id (NOT live — will recreate on next post)"; fi
}

usage() {
  cat <<'EOF'
answers.sh — the boss's durable answers feed (file + inbox + notification)

Subcommands:
  post --mgr MGR-N --subject "<title>" [--ticket N] --q "<q>" --a "<a>"
       [--no-inbox] [--no-notify]
  file            print the iCloud answers-file path
  inbox-id        print/ensure the inbox session cse_*
  inbox-status    show the recorded inbox cse_* and whether it is live
  path            print the answers state dir

Env: ANSWERS_FILE, ANSWERS_STATE_DIR, AI_HARNESS_DIR, ANSWERS_NOW
EOF
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    post)          cmd_post "$@" ;;
    file)          printf '%s\n' "$ANSWERS_FILE" ;;
    inbox-id)      _ensure_inbox ;;
    inbox-status)  cmd_inbox_status ;;
    path)          state_dir ;;
    -h|--help|help|"") usage ;;
    *) die "unknown subcommand: $sub" ;;
  esac
}

main "$@"
