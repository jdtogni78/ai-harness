# FINDINGS — Approach C: fully local / private voice loop

**Worker:** w3-voice-local · **Host:** Apple **M5**, macOS (Darwin 25.5), Python 3.13
**Pipeline:** mic → whisper.cpp (STT) → Claude (brain, 1 tool) → Piper (TTS) → speaker
**Privacy invariant:** audio never leaves the machine; only the utterance *text* is sent to Claude.

## Setup effort — **Low-to-moderate**
- STT: `brew install whisper-cpp` → drops `whisper-cli` (a binary). Uses Metal/CoreML
  on Apple Silicon with no config. One `curl` for the GGML model.
- TTS: `pip install piper-tts` → `piper` CLI + onnxruntime. One `curl` per voice.
- Mic: `brew install portaudio` + `pip install sounddevice`.
- Models total ~200MB (base.en 141MB + lessac-medium 60MB), no accounts/keys.
- Only secret in the whole system: `ANTHROPIC_API_KEY`.
- The POC **dry-runs with zero setup** via `--self-test` (uses the macOS `say`
  fallback), and runs `--text` mode with no mic. Real friction was ~20 min, mostly
  model downloads.

## Latency (measured on this M5, 3 runs each)
| Stage | Cold (first call) | Warm | Ratio (warm) |
|---|---|---|---|
| **STT** whisper.cpp base.en, 3.77s utterance | ~11.2s (one-time Metal shader warmup) | **~0.24s** | ~15× real-time |
| **TTS** Piper lessac-medium, 6.35s of speech | ~2.3s | **~0.65s** (full synth) | ~10× real-time |
| **Brain** Claude (Opus) | — | not measured here (no key in this env) | one network hop |

- **Local cost of a turn is tiny once warm: ~0.2–0.9s total** for STT+TTS synth.
  Piper can stream (`--output-raw`), so time-to-first-audio is lower than full-synth.
- The **cold start dominates the first turn** (~11s for whisper's Metal warmup). A
  one-line warmup call at startup hides this from the user — recommended.
- Practical end-to-end warm turn ≈ **STT 0.25s + brain ~1–3s (network) + TTS first
  audio ~0.5s ≈ ~2–4s to start replying**. The brain (network) is the bottleneck,
  not the local audio — same as every approach.

## Naturalness / barge-in
- **Piper (lessac-medium): good, clearly synthetic but pleasant and intelligible** —
  a real step above macOS `say`, below cloud neural TTS (ElevenLabs/OpenAI). Fine
  for a status-reporting assistant; swappable voice with no code change.
- **STT accuracy:** base.en is strong on normal speech but mis-hears domain proper
  nouns ("dstrader" → "the strater"). `small.en` fixes most of this at ~3× the
  compute (still real-time on M5). Recommend small.en for this app's jargon.
- **Barge-in:** implemented via energy-VAD monitoring during playback
  (`_speak_with_bargein`), which `stop()`s Piper/afplay on speech onset. Works, but
  energy VAD is coarse; a production build wants webrtcvad/Silero + AEC to avoid the
  TTS echoing into the mic. Half-duplex (afplay) barge-in is "stop on my voice,"
  not true overlapping conversation.

## Cost — **Near zero, and predictable**
- STT + TTS are free and offline (electricity only). No per-minute audio API bills,
  unlike cloud STT/TTS or speech-to-speech.
- Only spend is Claude tokens for the brain — identical to the cloud approaches and
  bounded (short, speakable replies capped at 1024 tokens).

## Privacy win — **The whole point, and it holds**
- Mic audio + synthesized speech are 100% on-device. Only the **text** transcript
  leaves, to Claude. No third-party audio processor ever hears the operator.
- Works fully offline **except** the brain. Could go 100% offline with a local LLM,
  at a big quality cost — not worth it while Claude is the brain.

## Verdict
**Best privacy + lowest/steadiest cost; local audio is effectively free and fast
(~0.2–0.9s warm). Naturalness trails cloud TTS but is fine for status reporting.
Pick Approach C when data sensitivity or offline/air-gapped-audio matters; pick a
cloud approach when you want the most natural voice and lowest engineering effort.**
The local audio stack is *not* the latency bottleneck — the network brain call is,
which is common to all approaches.

### Follow-ups / caveats
- Brain latency unmeasured in this sandbox (no `ANTHROPIC_API_KEY`); wiring is
  verified by `--self-test` and the tool-loop is standard SDK tool-use.
- Barge-in needs AEC to be robust with speakers (vs headphones).
- Consider `small.en` default for domain vocabulary; add a startup warmup call.
- Swap `status_stub.py` for W0's real MCP when published (shape already matches v0).
