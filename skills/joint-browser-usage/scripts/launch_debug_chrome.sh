#!/usr/bin/env bash
# launch_debug_chrome.sh — launch the boss's REAL Chrome with remote debugging
# on a DEDICATED profile, so the AI can later attach over CDP (see cdp_driver.py).
#
# Chrome 136+ blocks remote debugging on the DEFAULT profile, so a separate
# --user-data-dir is REQUIRED (and desirable anyway: it isolates this debug
# session from the boss's main Chrome). The boss logs in ONCE in this window
# (fresh profile => sign-in + 2FA + any Turnstile), and thereafter the AI drives
# already-authenticated pages by attaching to this same Chrome.
#
# Usage: launch_debug_chrome.sh [PORT] [PROFILE_DIR] [START_URL]
#   PORT         default 9222
#   PROFILE_DIR  default $HOME/.chrome-debug-ff
#   START_URL    default about:blank
set -euo pipefail

PORT="${1:-9222}"
PROFILE_DIR="${2:-$HOME/.chrome-debug-ff}"
START_URL="${3:-about:blank}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [ ! -x "$CHROME" ]; then
  echo "ERROR: Chrome not found at: $CHROME" >&2
  exit 1
fi

# Already up on this port? Reuse it — don't double-launch.
if curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "debug Chrome already listening on port ${PORT}:"
  curl -s "http://127.0.0.1:${PORT}/json/version"
  exit 0
fi

echo "launching debug Chrome: port=${PORT} profile=${PROFILE_DIR}"
"$CHROME" \
  --remote-debugging-port="${PORT}" \
  --user-data-dir="${PROFILE_DIR}" \
  "${START_URL}" >/dev/null 2>&1 &

# Wait for the debug endpoint to come up.
for _ in $(seq 1 20); do
  if curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    echo "up. verify:"
    curl -s "http://127.0.0.1:${PORT}/json/version"
    echo
    echo "Boss: log in in this Chrome window (clear any Turnstile / 2FA)."
    echo "Then the AI can attach: cdp_driver.py --port ${PORT}"
    exit 0
  fi
  sleep 0.5
done

echo "ERROR: debug endpoint did not come up on port ${PORT}" >&2
exit 1
