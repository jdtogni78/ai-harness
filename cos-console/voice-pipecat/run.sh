#!/usr/bin/env bash
# Convenience runner. Creates a venv on first use, installs deps, then runs.
#
#   ./run.sh dry           # keyless canned demo (no install needed for canned path)
#   ./run.sh dry-headless  # keyless demo, no speaker output (CI-safe)
#   ./run.sh live          # full mic->STT->Claude->TTS->speaker loop
#
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-dry}"
VENV=".venv"

# The canned dry-run needs nothing but the stdlib — run it straight from python3.
if [[ "$MODE" == "dry" && ! -d "$VENV" ]]; then
  exec python3 -m voice_pipecat --dry-run
fi
if [[ "$MODE" == "dry-headless" && ! -d "$VENV" ]]; then
  exec python3 -m voice_pipecat --dry-run --no-audio
fi

if [[ ! -d "$VENV" ]]; then
  echo "Creating venv + installing deps (first run; needs portaudio: brew install portaudio)..."
  python3 -m venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  pip install --upgrade pip
  pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
fi

case "$MODE" in
  dry)          exec python -m voice_pipecat --dry-run ;;
  dry-headless) exec python -m voice_pipecat --dry-run --no-audio ;;
  live)         exec python -m voice_pipecat ;;
  *) echo "usage: ./run.sh [dry|dry-headless|live]" >&2; exit 2 ;;
esac
