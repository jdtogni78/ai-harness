#!/bin/bash
# Size-based rotation for ai-harness logs.
#
# Driven by ~/Library/LaunchAgents/com.claudio1.claude-logrotate.plist (hourly).
# Picked over macOS newsyslog because that binary requires root even in
# user-space launchd contexts; this script runs fine as the invoking user.
#
# Strategy: gzip-copy current log to <log>.1.gz, then truncate (": > <log>")
# instead of rm. Truncate preserves the inode, so any process that has the
# log open with O_APPEND (as procutil.py:173 does — Python open(..., "a"))
# keeps writing safely; the next write goes to offset 0 of the same inode.
# rm would leak the old inode until every holder closed its fd.

set -u

LOGDIR="/Users/claudio1/dev/ai-harness/logs"
MAX_BYTES=$((10 * 1024 * 1024))   # 10 MiB
KEEP=3                            # keep <log>.1.gz .. <log>.3.gz

rotate_if_big() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    local size
    size=$(/usr/bin/stat -f %z "$f" 2>/dev/null) || return 0
    (( size > MAX_BYTES )) || return 0

    # Shift older rotations down: .2.gz -> .3.gz, .1.gz -> .2.gz, ...
    local i
    for (( i = KEEP - 1; i >= 1; i-- )); do
        if [[ -f "${f}.${i}.gz" ]]; then
            /bin/mv -f "${f}.${i}.gz" "${f}.$((i+1)).gz"
        fi
    done

    # Snapshot current contents to .1.gz, then atomically zero the file.
    # gzip-c can race with concurrent appends — that's fine: we accept losing
    # the trailing few writes during the snapshot rather than stopping the
    # producing process. The truncate happens after gzip exits.
    if /usr/bin/gzip -c "$f" > "${f}.1.gz.tmp" 2>/dev/null; then
        /bin/mv -f "${f}.1.gz.tmp" "${f}.1.gz"
        : > "$f"
        printf '%s rotated %s (was %s bytes)\n' \
            "$(/bin/date '+%Y-%m-%d %H:%M:%S')" "$f" "$size"
    else
        /bin/rm -f "${f}.1.gz.tmp" 2>/dev/null
        printf '%s gzip failed for %s\n' \
            "$(/bin/date '+%Y-%m-%d %H:%M:%S')" "$f"
    fi
}

# Explicit list (no globbing) so a typo / new log file pattern doesn't get
# rotated by accident. Add to this when a new log path is introduced.
for f in \
    "$LOGDIR"/mini-dev.log \
    "$LOGDIR"/mini-FamilyFund.log \
    "$LOGDIR"/mini-ai-harness.log \
    "$LOGDIR"/mini-dstrader.log \
    "$LOGDIR"/mini-dstrader-docker.log \
    "$LOGDIR"/mini-job-search.log \
    "$LOGDIR"/manager.log \
    "$LOGDIR"/perm-gate.log \
    "$LOGDIR"/perm-gate-decisions.jsonl \
    "$LOGDIR"/usage-limit-monitor.log \
    "$LOGDIR"/titles-monitor.log \
    "$LOGDIR"/launchd.err.log \
    "$LOGDIR"/launchd.out.log
do
    rotate_if_big "$f"
done
