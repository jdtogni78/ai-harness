#!/usr/bin/env bash
# Fetch local STT + TTS models into ./models. No secrets needed; ~200MB total.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="$ROOT/models"
mkdir -p "$MODELS"

fetch() {
  # fetch <url> <dest-filename>
  local url="$1" dest="$MODELS/$2"
  if [ -f "$dest" ]; then
    echo "have $2"
  else
    echo "downloading $2 ..."
    curl -L --fail -o "$dest" "$url"
  fi
}

# --- Whisper.cpp GGML model ---
# base.en (~141MB): fast, great for command-style speech.
# For higher accuracy, also fetch ggml-small.en.bin (~488MB) and set VL_WHISPER_MODEL.
WHISPER="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
fetch "$WHISPER/ggml-base.en.bin" "ggml-base.en.bin"

# --- Piper voice: en_US-lessac-medium (clean, natural US English) ---
PIPER="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
fetch "$PIPER/en_US-lessac-medium.onnx" "en_US-lessac-medium.onnx"
fetch "$PIPER/en_US-lessac-medium.onnx.json" "en_US-lessac-medium.onnx.json"

echo
echo "done. models in $MODELS"
echo "STT: base.en  |  TTS: en_US-lessac-medium"
