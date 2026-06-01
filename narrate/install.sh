#!/usr/bin/env bash
# Create narrate/.venv and install Playwright + Chromium. Idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
"$VENV/bin/python" -m playwright install chromium

echo "narrate: ready. Run via $HERE/narrate or $VENV/bin/python -m narrate ..."
