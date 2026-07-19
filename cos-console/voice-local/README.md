# voice-local — fully-local voice loop (Approach C)

A privacy-first voice "chief of staff" for the **cos-console** exploration.
**The audio never leaves the machine.** The only network hop is the Claude API
call (the brain).

```
 mic ──► whisper.cpp (STT) ──► Claude (brain, 1 tool) ──► Piper (TTS) ──► speaker
 └────────────── all on-device ──────────────┘   │   └──── all on-device ────┘
                                          the one network hop
```

Claude is the brain and calls exactly one tool — `get_project_status(project)` —
per the shared status contract in `../PROJECT.md`. Until sibling **w0-data-plane**
publishes the real MCP, that tool is **stubbed** against the v0 `StatusReport`
shape (`voice_local/status_stub.py`).

Target platform: **macOS, Apple Silicon** (built + measured on an Apple M5).

---

## Quick start

### 0. Dry-run with zero setup (no key, no models)
Validates the whole pipeline's wiring and speaks a line via the local macOS voice:
```bash
python3 -m voice_local.main --self-test
```

### 1. Install local components
```bash
# STT engine + mic I/O (Homebrew)
brew install whisper-cpp portaudio

# Python deps + Piper TTS (use a venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Local models (~200MB: whisper base.en + Piper lessac-medium). No secrets needed.
./scripts/download_models.sh
```

### 2. Add the one secret (the brain)
```bash
cp .env.example .env
# edit .env, set ANTHROPIC_API_KEY=...
```

### 3. Run it
```bash
# Full voice loop: speak, it listens/transcribes/thinks/replies out loud
python3 -m voice_local.main

# Type instead of speaking (no mic needed; still local STT-free + real brain + local TTS)
python3 -m voice_local.main --text

# One-shot, great for timing a turn
python3 -m voice_local.main --say "How is dstrader doing? Did it deploy to prod?"

# Print replies instead of speaking them
python3 -m voice_local.main --text --no-voice-out
```

Try: *"How many tests are passing on dstrader?"*, *"Did cos-console deploy to
prod?"*, *"What decisions were made on dstrader recently?"*, *"Any open questions?"*

---

## Component choices (and why)

| Stage | Choice | Model | Why |
|---|---|---|---|
| **STT** | whisper.cpp (`whisper-cli`) | `ggml-base.en.bin` (~141MB) | Uses Metal/CoreML on Apple Silicon automatically; ~15× real-time warm. `small.en` (~488MB) is more accurate on proper nouns — set `VL_WHISPER_MODEL`. |
| **Brain** | Claude via Anthropic SDK | `claude-opus-4-8` | Tool-use decides what to say. One tool only, per the shared contract. |
| **TTS** | Piper | `en_US-lessac-medium` (~60MB) | Fast, natural, fully offline. Falls back to macOS `say` (also local) if Piper/voice missing, so it always talks. |

Everything except the Claude call is on-device. Swap voices via `VL_PIPER_VOICE`
(browse [piper-voices](https://huggingface.co/rhasspy/piper-voices)); e.g.
`en_US-amy-medium`, `en_GB-alan-medium`. Set `VL_TTS_ENGINE=say` to hear the
zero-install fallback.

---

## How it works

- **`audio.py`** — mic capture with a simple energy VAD (RMS threshold + trailing
  silence). Also `monitor_speech()` for **barge-in**: it listens while the TTS is
  talking and cuts playback the moment you start speaking.
- **`stt.py`** — pipes 16k mono audio to `whisper-cli`, returns text + timing.
- **`brain.py`** — Claude with the single `get_project_status` tool and the full
  tool-use loop. System prompt keeps replies short and speakable (no URLs/JSON).
- **`tts.py`** — Piper (streaming raw audio to `ffplay`/`sox` if present, else a
  temp WAV + `afplay`), with the `say` fallback. Returns a handle you can `stop()`.
- **`status_stub.py`** — the v0 `StatusReport` stub. **W0 owns the real schema**;
  when its MCP lands, swap `get_project_status` to call it and delete this file.

## Config

All knobs are env vars (see `.env.example` / `voice_local/config.py`):
`VL_MODEL`, `VL_WHISPER_MODEL`, `VL_TTS_ENGINE`, `VL_PIPER_VOICE`, `VL_SAY_VOICE`,
`VL_VAD_SILENCE_MS`, `VL_VAD_RMS`.

## Tests
```bash
python3 -m voice_local.main --self-test          # wiring + local TTS, no key/models
python3 -c "import tests.test_status_stub as t; \
  [getattr(t,f)() for f in dir(t) if f.startswith('test_')]; print('ok')"
```

## Privacy note
Mic audio and synthesized speech are processed entirely on-device. Only the
**text** transcript of your utterance (plus stubbed status data) is sent to the
Claude API. No audio ever leaves the machine. See `FINDINGS.md` for the trade-off
analysis vs the cloud approaches.
